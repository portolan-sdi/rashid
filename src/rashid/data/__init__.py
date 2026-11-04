"""Data pass: verify asset bytes against what the metadata declares.

The metadata pass checks that assets *declare* the required fields — an
``href``, a media ``type``, a positive ``file:size``, a well-formed
``file:checksum`` (``PTL-AST-001..004``). None of that opens the asset, so a
catalog can declare a checksum, size, format, or extent that its actual bytes
contradict and still pass. core.md makes several of these a hard MUST:
``file:checksum``/``file:size`` "MUST be regenerated at publish time … so they
always match what is in the bucket", and each format carries a required media
type.

This pass reads the bytes — local files and remote ``https`` assets — and
reports where they diverge from the claim. It runs by default; CLI ``--no-data``
or ``ValidationOptions(data=False)`` skips it. It reaches the network for remote
assets and downgrades to a single WARNING when it cannot run.

Which bytes it may read is the caller's choice. ``reader_factory`` supplies the
:class:`~rashid.data.reader.AssetReader`, defaulting to
:class:`~rashid.data.reader.FilesystemHttpReader`; passing
:class:`~rashid.data.reader.LocalOnlyReader` (CLI ``--data-scope local``) keeps
the checks that read the catalog's own files and skips the ones that would have
to stream someone else's host.

The pass checks objects on a thread pool and reports in catalog order. On a
published catalog each asset read waits on the network, and a serial loop
over thousands of assets took hours (#219).

The heavy geospatial dependencies (``pyarrow``, ``rasterio``, ``rio-cogeo``,
``pyproj``) are core dependencies but are imported
lazily by :func:`default_validator`, so the core package stays stdlib-only and a
catalog with no data pass installs nothing new.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any

from rashid import _trace
from rashid.catalog import CatalogGraph, Kind, Node
from rashid.data.reader import AssetReader, FilesystemHttpReader, Locator
from rashid.model import Finding, Severity
from rashid.registry import DAT_UNAVAILABLE
from rashid.rules._common import assets_of

# Assets are declared on collections and items; catalogs carry none.
_DATA_KINDS: tuple[Kind, ...] = ("collection", "item")

#: Objects with a remote asset checked at once. A remote asset costs several
#: requests that each wait hundreds of milliseconds for a first byte (#219).
#: obstore releases the GIL while it waits, and so does GDAL when it opens or
#: reads a dataset. GDAL's sidecar probes run inside rasterio calls that keep
#: the GIL, which is one reason checks.py turns them off for a remote asset.
#: The threads then overlap those waits rather than queue on them.
_REMOTE_WORKERS = 32

#: Objects whose assets are all local checked at once. Parsing a footer and
#: judging row order is Python work that holds the GIL, so threads past a few
#: only contend for it: on the large ``data`` scenario the pass took 36 ms on
#: 4 threads, 43 ms on 8, and 60 ms on 32. A few still overlap file reads and
#: the hashing of large files, which release it.
_LOCAL_WORKERS = 4


@dataclass(frozen=True)
class DataDefect:
    """One byte-vs-metadata mismatch for a single asset.

    A lightweight seed that :func:`validate_data` turns into a :class:`Finding`,
    filling in the object's path and id. ``asset_key`` and ``field`` locate the
    asset within the object (``/assets/<key>[/<field>]``); a node-level defect
    sets ``json_pointer`` directly instead. Two checks do that: the partition
    check, which binds to no single asset, and the consistency check when the
    contradicted ``proj:epsg`` is inherited from the enclosing object rather
    than declared on the asset.
    """

    rule_id: str
    severity: Severity
    message: str
    asset_key: str
    field: str | None = None
    json_pointer: str | None = None
    expected: Any | None = None
    actual: Any | None = None


class _DataResults:
    """Finding sources; each build derives shared-field and partition results anew."""

    def __init__(self) -> None:
        self._sources: list[tuple[DataDefect, int | None]] = []

    def extend(self, defects: list[DataDefect], *, partition_total: int | None = None) -> None:
        self._sources.extend((defect, partition_total) for defect in defects)

    def build(self) -> list[DataDefect]:
        groups: dict[tuple[object, ...], list[DataDefect]] = {}
        totals: dict[tuple[object, ...], int] = {}
        signature: tuple[object, ...]
        for index, (defect, total) in enumerate(self._sources):
            if total is not None:
                signature = ("partition", defect.rule_id, defect.severity)
                totals[signature] = total
            elif defect.json_pointer is not None:
                signature = ("field", defect.rule_id, defect.json_pointer, defect.message)
            else:
                signature = ("asset", index)
            groups.setdefault(signature, []).append(defect)
        results: list[DataDefect] = []
        for signature, members in groups.items():
            first = members[0]
            if signature[0] != "partition":
                results.append(first)
                continue
            message = first.message
            if len(members) > 1:
                message = (
                    f"{len(members)} of {totals[signature]} partition files fail this check; "
                    f"e.g. {first.message}"
                )
            results.append(
                DataDefect(
                    first.rule_id, first.severity, message, "", json_pointer="/partition:glob"
                )
            )
        return results


# A validator inspects one object's assets, given a reader for their bytes, and
# returns the mismatches it found; an empty list means every asset matched.
Validator = Callable[[Node, AssetReader], list[DataDefect]]

# A reader is built per run, from the graph whose tree it resolves hrefs
# against; both readers' constructors are therefore factories themselves.
ReaderFactory = Callable[[CatalogGraph], AssetReader]


def default_validator(graph: CatalogGraph | None = None) -> Validator:
    """Build the byte-verifying validator, importing the geospatial deps lazily.

    The metadata pass never needs these packages; importing :mod:`rashid.data.checks`
    pulls ``pyarrow``/``rasterio``/``pyproj``/``rio_cogeo``. A missing
    extra surfaces here as ``ImportError`` and is downgraded to one WARNING.

    ``graph`` is bound into the returned validator for the one check that needs
    more than a single object: the item mirror must agree with the collection's
    items, which means reading every item beneath the collection
    (``PTL-DAT-016``).
    Called without it, that check is skipped and the rest run unchanged.
    """
    from rashid.data import checks

    if graph is None:
        return checks.check_node
    return partial(checks.check_node, graph=graph)


def validate_data(
    graph: CatalogGraph,
    validator: Validator | None = None,
    *,
    reader_factory: ReaderFactory | None = None,
) -> list[Finding]:
    """Verify every asset's bytes against its declared metadata.

    Returns ``PTL-DAT-00x`` findings for each mismatch. If the validator is
    unavailable (the geospatial stack cannot import) or a call fails
    systemically, returns a single ``PTL-DAT-000`` warning instead of failing the
    run — a systemic failure is reported once, not once per object.

    An object with a remote asset is checked on a pool of ``_REMOTE_WORKERS``
    threads, and any other on a pool of ``_LOCAL_WORKERS``; the two run at
    once. Findings are emitted in catalog order, so the report does not depend on
    which thread finishes first. The first object whose validator raises ends
    the pass as the serial loop did: the findings of the objects before it are
    kept, the objects not yet started are cancelled, and nothing after it is
    reported. The objects still running then fail at their next call to the
    reader, so a whole-object stream stops within one chunk. A ``validator`` and the reader ``reader_factory`` builds are
    therefore called from several threads at once, and must be thread-safe.

    ``reader_factory`` chooses which assets are readable at all;
    :class:`~rashid.data.reader.LocalOnlyReader` narrows the pass to the
    catalog's own files. An asset its reader declines is skipped silently, so a
    narrowed scope produces a subset of the findings rather than new ones.
    """
    if validator is None:
        try:
            validator = default_validator(graph)
        except ImportError:
            return [
                Finding(
                    rule_id=DAT_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=(
                        "data validation skipped: the geospatial stack could not be imported "
                        "(needs pyarrow, rasterio, rio-cogeo, and pyproj; pass data=False "
                        "or --no-data to skip byte checks)"
                    ),
                    path=".",
                )
            ]

    reader = (reader_factory or FilesystemHttpReader)(graph)
    nodes = [node for node in graph.iter(*_DATA_KINDS) if node.parse_error is None]
    findings: list[Finding] = []
    ended = threading.Event()
    shared = _EndableReader(reader, ended)
    remote = ThreadPoolExecutor(_REMOTE_WORKERS, thread_name_prefix="data-remote")
    local = ThreadPoolExecutor(_LOCAL_WORKERS, thread_name_prefix="data-local")
    try:
        futures = [
            _trace.submit(remote if _reads_remote(node, reader) else local, validator, node, shared)
            for node in nodes
        ]
        for node, future in zip(nodes, futures, strict=True):
            try:
                defects = future.result()
            except Exception as exc:  # any validator failure is systemic
                findings.append(
                    Finding(
                        rule_id=DAT_UNAVAILABLE,
                        severity=Severity.WARNING,
                        message=f"data validation could not run: {exc}",
                        path=".",
                    )
                )
                return findings
            findings.extend(_to_findings(node, defects))
    finally:
        # Every future is done on the normal path. On a systemic failure or an
        # interrupt, drop the objects not yet started and return without
        # waiting. An object already reading may be streaming gigabytes for a
        # checksum, and its result would be thrown away. The pool threads are
        # not daemons, so the interpreter joins them at exit: ending the reader
        # makes each one stop at its next chunk or reader call.
        ended.set()
        for pool in (remote, local):
            pool.shutdown(wait=False, cancel_futures=True)
    return findings


class _PassEnded(Exception):
    """A worker read after :func:`validate_data` stopped waiting for it."""


class _EndableReader:
    """``reader``, until ``ended`` is set; then every call and chunk raises.

    GDAL's own reads of a ``/vsicurl`` raster bypass this reader. They fetch
    a header and a few tiles, not the whole object, so they end soon anyway.
    """

    def __init__(self, reader: AssetReader, ended: threading.Event) -> None:
        self._reader = reader
        self._ended = ended

    def _check(self) -> None:
        if self._ended.is_set():
            raise _PassEnded("the data pass ended before this object finished")

    def locate(self, node: Node, href: str) -> Locator | None:
        self._check()
        return self._reader.locate(node, href)

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        self._check()
        stream = self._reader.stream(node, href)
        return None if stream is None else self._chunks(stream)

    def _chunks(self, stream: Iterator[bytes]) -> Iterator[bytes]:
        try:
            for chunk in stream:
                self._check()
                yield chunk
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()


def _reads_remote(node: Node, reader: AssetReader) -> bool:
    """Whether any asset on ``node`` resolves to a remote URL under ``reader``.

    Locating is the reader's own answer, so a narrowed reader such as
    :class:`~rashid.data.reader.LocalOnlyReader` sends every object to the
    local pool. A reader that fails here sends the object to the local pool
    too, where the validator meets the same failure and the pass reports it
    as a systemic one.
    """
    for _pointer, _key, asset in assets_of(node):
        href = asset.get("href")
        if isinstance(href, str) and href:
            try:
                located = reader.locate(node, href)
            except Exception:  # the validator reports it
                return False
            if located is not None and located.is_remote:
                return True
    return False


def _to_findings(node: Node, defects: list[DataDefect]) -> list[Finding]:
    findings: list[Finding] = []
    for defect in defects:
        pointer = defect.json_pointer
        if pointer is None:
            pointer = f"/assets/{defect.asset_key}"
            if defect.field is not None:
                pointer = f"{pointer}/{defect.field}"
        findings.append(
            Finding(
                rule_id=defect.rule_id,
                severity=defect.severity,
                message=defect.message,
                path=str(node.path),
                object_id=node.id,
                json_pointer=pointer,
                expected=defect.expected,
                actual=defect.actual,
            )
        )
    return findings
