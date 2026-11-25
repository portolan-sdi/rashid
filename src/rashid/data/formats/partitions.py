"""Partition enumeration, local and in a bucket, and schema comparisons."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote

from rashid import _store
from rashid._href import bucket_glob, is_absolute_href, is_https_href, split_href
from rashid._stac import assets_of
from rashid.catalog import CatalogGraph, Node
from rashid.data._results import DataDefect
from rashid.data.formats import _files
from rashid.data.formats._files import AssetFiles
from rashid.data.formats.geoparquet import check_geoparquet, partitioned_vector_columns
from rashid.data.reader import BUCKET_PROBE, AssetReader, Locator
from rashid.ids import DAT_ORDERING, DAT_PARTITION_SCHEMA, DAT_UNAVAILABLE
from rashid.model import Severity, json_pointer

_T = TypeVar("_T")

# --- partition schema consistency ------------------------------------------


def partition_files(node: Node) -> list[tuple[str, Path]]:
    """Local files the collection's ``partition:glob`` matches, sorted.

    Only a relative glob is expanded, against the collection's own directory.
    A conformant glob is an absolute bucket URL (``s3://``), which rashid does
    not list, so it yields nothing, as does any other absolute pattern or one
    that normalizes outside the catalog. PTL-PRT-002 reports the relative glob.
    Each entry pairs the relative match, which names the file in a message,
    with its path on disk.
    """
    pattern = node.data.get("partition:glob")
    if not isinstance(pattern, str) or not pattern.strip():
        return []
    if is_absolute_href(pattern):
        return []  # remote or absolute: not listable from here
    normalized = posixpath.normpath(pattern)
    if normalized.startswith(".."):
        return []  # escapes the catalog tree
    base = node.abs_path.parent
    return _files.partition_files(base, normalized)


def check_partitions(
    node: Node, reader: AssetReader, graph: CatalogGraph | None = None
) -> tuple[list[DataDefect], list[tuple[list[DataDefect], int]]]:
    """Read each partition's schema and GeoParquet checks through one handle.

    Returns the collection-level findings, and groups of per-file findings
    with the number of files each group covers, which the report folds into
    one finding per rule. A relative glob is expanded on disk and every file
    it matches is read. A bucket glob is listed and sampled
    (:func:`_check_bucket_glob`).

    Only the column maps survive each file, so an arbitrarily large glob does
    not retain an open descriptor per partition. Declared assets are compared
    for schema here; their format findings come from the asset pass alone.
    """
    glob = node.data.get("partition:glob")
    target = bucket_glob(glob) if isinstance(glob, str) else None
    if target is not None:
        return _check_bucket_glob(node, reader, graph, target)
    files = partition_files(node)
    if not files:
        return [], []
    already_read = _asset_paths(node, reader)
    sources = [
        (match, Locator(is_remote=False, source=str(path)), str(path) in already_read)
        for match, path in files
    ]
    collection, found = _read_partitions(node, sources, rows=None)
    return collection, [(found, len(files))]


def _read_partitions(
    node: Node, sources: list[tuple[str, Locator, bool]], *, rows: set[str] | None
) -> tuple[list[DataDefect], list[DataDefect]]:
    """Collection-level findings and per-file findings for ``sources``.

    Each source is ``(name, locator, declared)``; a declared file is an asset
    the asset pass already checks, so only its schema is compared here.
    ``rows`` names the files whose rows the ordering check may read; None
    means every file.
    """
    schemas: list[tuple[str, dict[str, str]]] = []
    found: list[DataDefect] = []
    collection: list[DataDefect] = []
    for match, located, declared in sources:
        with AssetFiles(located) as opened:
            try:
                parquet = opened.parquet()
            except Exception as exc:
                found.append(
                    DataDefect(
                        DAT_UNAVAILABLE,
                        Severity.WARNING,
                        f"partition file '{match}' data validation could not run: {exc}",
                        match,
                    )
                )
                continue
            if not schemas:
                collection.extend(partitioned_vector_columns(node, parquet))
            schemas.append((match, {field.name: str(field.type) for field in parquet.schema_arrow}))
            if not declared:
                found.extend(
                    check_geoparquet(
                        match,
                        opened,
                        subject=f"partition file '{match}'",
                        rows=rows is None or match in rows,
                    )
                )
    return collection + _partition_schema_defects(schemas), found


# --- bucket globs --------------------------------------------------------------

# The most objects one listing returns, and how many matched files are read:
# the footers of a spread-out sample, and the rows of a smaller one.
_LIST_CAP = 10_000
_FOOTER_SAMPLE = 64
_ROW_SAMPLE = 4

_TEMPLATE_FIELD = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class _Bucket:
    """How to list a bucket anonymously, and the https URL its keys sit under."""

    config: dict[str, Any]
    base: str


def _check_bucket_glob(
    node: Node, reader: AssetReader, graph: CatalogGraph | None, target: tuple[str, str, str]
) -> tuple[list[DataDefect], list[tuple[list[DataDefect], int]]]:
    """List an ``s3://``, ``gs://``, or ``az://`` glob and check a sample of its files.

    Clients read a partitioned dataset by listing its bucket (formats.md,
    Partitioned Collections), so the listing is anonymous. The footers of up
    to ``_FOOTER_SAMPLE`` evenly spaced files, always the first and the last,
    are read for the schema and footer rules, and the rows of
    ``_ROW_SAMPLE`` of them for the ordering check. A listing that is denied,
    finds nothing, or matches nothing is a ``PTL-DAT-000`` warning. A sample
    and a refusal by policy are ``PTL-DAT-000`` infos.
    """
    scheme, bucket, pattern = target
    access = _bucket_access(node, graph, scheme, bucket, reader)
    if access is None:
        return [], []  # the reader is scoped to local files
    if isinstance(access, DataDefect):
        return [access], []
    try:
        matched, listed, truncated = _files.remote_partition_files(
            scheme, bucket, pattern, limit=_LIST_CAP, config=access.config
        )
    except _store.StoreError as exc:
        why = {401: "denied", 403: "denied", 404: "not found"}.get(exc.status or 0, "failed")
        return [_glob_note(Severity.WARNING, f"listing the glob {why}: {exc}")], []
    except re.error as exc:
        return [_glob_note(Severity.WARNING, f"the glob is not a valid pattern: {exc}")], []
    if not matched:
        return [
            _glob_note(
                Severity.WARNING,
                f"the glob matches none of the {listed} object(s) listed under it"
                + (", and the listing stopped there" if truncated else ""),
            )
        ], []
    return _check_sample(node, access, matched, listed if truncated else None)


def _check_sample(
    node: Node, access: _Bucket, matched: list[tuple[str, int]], stopped_at: int | None
) -> tuple[list[DataDefect], list[tuple[list[DataDefect], int]]]:
    """Read the sampled footers and rows; one info says how much was read.

    ``stopped_at`` is the listing's size when it stopped at its cap.
    """
    footers = _evenly_spaced(matched, _FOOTER_SAMPLE)
    rows = {key for key, _size in _evenly_spaced(footers, _ROW_SAMPLE)}
    notes = []
    if stopped_at is not None or len(footers) < len(matched):
        stopped = "" if stopped_at is None else f", and the listing stopped at {stopped_at}"
        notes.append(
            _glob_note(
                Severity.INFO,
                f"read the footers of {len(footers)} of {len(matched)} matched partition"
                f" files and the rows of {len(rows)}{stopped}",
            )
        )
    sources = [
        (key, Locator(is_remote=True, source=access.base + quote(key, safe="/"), size=size), False)
        for key, size in footers
    ]
    collection, found = _read_partitions(node, sources, rows=rows)
    ordering = [d for d in found if d.rule_id == DAT_ORDERING]
    footer = [d for d in found if d.rule_id != DAT_ORDERING]
    return notes + collection, [(footer, len(footers)), (ordering, len(rows))]


def _bucket_access(
    node: Node, graph: CatalogGraph | None, scheme: str, bucket: str, reader: AssetReader
) -> _Bucket | DataDefect | None:
    """How to list ``bucket``, from the ``storage:schemes`` entry that names it.

    The entry may sit on the collection or on any ancestor. Without one, an
    ``s3`` bucket is Amazon S3 and a ``gs`` bucket is Google Cloud Storage.
    A refusal by policy (requester pays, no Azure account, a template field
    with no value, an unsupported type) is an info. None when ``reader`` reads
    no remote file, such as with ``--data-scope local``; then no request goes out.
    """
    if reader.locate(node, BUCKET_PROBE) is None:
        return None
    entry = _storage_scheme(node, graph, scheme, bucket)
    kind = entry.get("type") if entry else _DEFAULT_TYPES.get(scheme)
    if entry and entry.get("requester_pays") is True:
        return _glob_note(Severity.INFO, "not listed: the bucket is requester pays")
    build = _ACCESS.get((scheme, str(kind)))
    if build is None:
        what = f"a storage scheme of type '{kind}'" if kind else "no storage:schemes entry"
        return _glob_note(
            Severity.INFO, f"not listed: rashid cannot list a {scheme} glob with {what}"
        )
    return build(entry or {}, bucket)


def _aws_s3(entry: dict[str, Any], bucket: str) -> _Bucket | DataDefect:
    """Amazon S3: the region comes from the entry, or from the bucket itself."""
    region = entry.get("region")
    region = region if isinstance(region, str) else _store.bucket_region(bucket)
    if region is None:
        return _glob_note(
            Severity.WARNING, f"listing the glob failed: no S3 region found for {bucket}"
        )
    return _Bucket({"region": region}, f"https://s3.{region}.amazonaws.com/{bucket}/")


def _gcs(_entry: dict[str, Any], bucket: str) -> _Bucket | DataDefect:
    return _Bucket({}, f"https://storage.googleapis.com/{bucket}/")


def _azure(entry: dict[str, Any], bucket: str) -> _Bucket | DataDefect:
    """Azure Blob Storage: the container sits under the entry's account."""
    account = entry.get("account")
    if not isinstance(account, str) or not account:
        return _glob_note(Severity.INFO, "not listed: the ms-azure storage scheme names no account")
    return _Bucket({"account_name": account}, f"https://{account}.blob.core.windows.net/{bucket}/")


def _custom_s3(entry: dict[str, Any], bucket: str) -> _Bucket | DataDefect:
    """An S3-compatible service: its endpoint is the expanded ``platform`` template."""
    platform = entry.get("platform")
    fields = {**entry, "bucket": bucket}
    endpoint = _TEMPLATE_FIELD.sub(
        lambda m: str(fields[m[1]]) if fields.get(m[1]) is not None else m[0],
        platform if isinstance(platform, str) else "",
    ).rstrip("/")
    if not is_https_href(endpoint) or _TEMPLATE_FIELD.search(endpoint):
        return _glob_note(
            Severity.INFO,
            f"not listed: the custom-s3 platform {platform!r} does not resolve to an https URL",
        )
    region = entry.get("region")
    config: dict[str, Any] = {"endpoint": endpoint}
    if isinstance(region, str):
        config["region"] = region
    template = split_href(str(platform))
    if template is not None and "{bucket}" in template.netloc:
        config["virtual_hosted_style_request"] = True
        return _Bucket(config, f"{endpoint}/")
    return _Bucket(config, f"{endpoint}/{bucket}/")


# Without a storage:schemes entry, an s3 bucket is Amazon S3 and a gs bucket is
# Google Cloud Storage. "gcs" is rashid's own name; the extension has no type for it.
_DEFAULT_TYPES = {"s3": "aws-s3", "gs": "gcs"}
_ACCESS = {
    ("s3", "aws-s3"): _aws_s3,
    ("s3", "custom-s3"): _custom_s3,
    ("gs", "gcs"): _gcs,
    ("az", "ms-azure"): _azure,
}


def _storage_scheme(
    node: Node, graph: CatalogGraph | None, scheme: str, bucket: str
) -> dict[str, Any] | None:
    """The ``storage:schemes`` entry for ``bucket``, on ``node`` or an ancestor.

    An entry that names the bucket (or Azure container) wins. A Storage
    v2.0.0 ``ms-azure`` entry names only its account, so for an ``az`` glob
    the first such entry without a bucket is the fallback.
    """
    fallback = None
    current: Node | None = node
    while current is not None:
        schemes = current.data.get("storage:schemes")
        entries = schemes.values() if isinstance(schemes, dict) else ()
        for entry in (e for e in entries if isinstance(e, dict)):
            if bucket in (entry.get("bucket"), entry.get("container")):
                return entry
            unnamed = "bucket" not in entry and "container" not in entry
            if fallback is None and scheme == "az" and unnamed and entry.get("type") == "ms-azure":
                fallback = entry
        current = graph.parent_of(current) if graph is not None else None
    return fallback


def _evenly_spaced(items: list[_T], count: int) -> list[_T]:
    """``count`` items spread across ``items``, always the first and the last."""
    if len(items) <= count:
        return items
    if count == 1:
        return items[:1]
    picks = sorted({round(i * (len(items) - 1) / (count - 1)) for i in range(count)})
    return [items[i] for i in picks]


def _glob_note(severity: Severity, message: str) -> DataDefect:
    return DataDefect(
        DAT_UNAVAILABLE,
        severity,
        f"partition:glob: {message}",
        "",
        json_pointer=json_pointer("partition:glob"),
    )


def _partition_schema_defects(schemas: list[tuple[str, dict[str, str]]]) -> list[DataDefect]:
    """Every listed partition MUST share the first readable file's schema."""
    if len(schemas) < 2:
        return []  # nothing to compare
    reference_name, reference = schemas[0]
    defects: list[DataDefect] = []
    for name, schema in schemas[1:]:
        difference = _schema_difference(reference, schema)
        if difference is None:
            continue
        defects.append(
            DataDefect(
                DAT_PARTITION_SCHEMA,
                Severity.ERROR,
                f"partition file '{name}' does not share '{reference_name}'s "
                f"Parquet schema: {difference}",
                "",
                json_pointer=json_pointer("partition:glob"),
            )
        )
    return defects


def _asset_paths(node: Node, reader: AssetReader) -> set[str]:
    """Local paths the per-asset loop already reads on this node.

    A glob wide enough to match a declared asset would otherwise fault the same
    file twice, once per pass.
    """
    paths: set[str] = set()
    for _pointer, _key, asset in assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            continue
        located = reader.locate(node, href)
        if located is not None and not located.is_remote:
            paths.add(located.source)
    return paths


def _schema_difference(reference: dict[str, str], other: dict[str, str]) -> str | None:
    """A one-line description of how two column maps diverge, or None if equal."""
    missing = sorted(set(reference) - set(other))
    extra = sorted(set(other) - set(reference))
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing column(s) {missing}")
        if extra:
            parts.append(f"extra column(s) {extra}")
        return ", ".join(parts)
    for name in reference:
        if reference[name] != other[name]:
            return f"column '{name}' is {other[name]}, expected {reference[name]}"
    return None
