"""Validation runner: build the graph, run the rules, produce a report."""

from __future__ import annotations

import dataclasses
import logging
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from rashid import _trace
from rashid._href import is_catalog_url, split_catalog_url
from rashid._jsonschema import SchemaError
from rashid.catalog import ROOT_CATALOG, CatalogGraph
from rashid.config import RulesConfig
from rashid.data import ReaderFactory as DataReaderFactory
from rashid.data import Validator as DataValidator
from rashid.data import validate_data
from rashid.extensions import validate_extensions
from rashid.live import Prober as LiveProber
from rashid.live import validate_live
from rashid.model import Finding, Report, Severity
from rashid.registry import (
    DEFAULT_RULES,
    GEN_MISSING_ROOT,
    GEN_PARTIAL_TREE,
    GEN_UNPARSEABLE,
    check_ids,
)
from rashid.remote import (
    DEFAULT_MAX_DOCUMENTS,
    Crawl,
    Fetcher,
    FetchError,
    crawl,
)
from rashid.rule import Rule
from rashid.schema import validate_schema
from rashid.structural import validate_structural

log = logging.getLogger(__name__)


# Structural/schema validators map one object's raw JSON to schema errors.
_Validator = Callable[[dict[str, Any]], list[SchemaError]]


@dataclasses.dataclass(frozen=True, kw_only=True)
class ValidationOptions:
    """Validation settings and optional adapters shared by local and HTTPS inputs.

    Pass one instance as ``validate(path, options=ValidationOptions(...))``.
    ``rules=None`` selects the default metadata rules; an empty sequence skips them.
    ``live=None`` enables hosting checks for URLs and disables them on disk.
    Validator, reader, prober, and fetcher adapters chiefly support offline tests.
    """

    config: RulesConfig | None = None
    rules: Sequence[Rule] | None = None
    structural: bool = True
    structural_validator: _Validator | None = None
    extensions: bool = True
    schema: bool = False
    schema_validator: _Validator | None = None
    schema_allow_network: bool = False
    data: bool = True
    data_validator: DataValidator | None = None
    data_reader_factory: DataReaderFactory | None = None
    live: bool | None = None
    live_prober: LiveProber | None = None
    live_base_url: str | None = None
    fetcher: Fetcher | None = None
    max_documents: int = DEFAULT_MAX_DOCUMENTS


def _optional_passes(
    graph: CatalogGraph,
    config: RulesConfig,
    options: ValidationOptions,
    *,
    known_statuses: Mapping[str, int] | None = None,
) -> list[Finding]:
    """Run the structural, extension, schema, data, and live passes, honouring disable ids."""
    findings: list[Finding] = []
    if options.structural and not config.disabled >= check_ids(
        "structural", enables_pass_only=True
    ):
        findings.extend(
            _stage("structural", lambda: validate_structural(graph, options.structural_validator))
        )
    if options.extensions and not config.disabled >= check_ids(
        "extensions", enables_pass_only=True
    ):
        findings.extend(
            f
            for f in _stage(
                "extensions",
                lambda: validate_extensions(graph, allow_network=options.schema_allow_network),
            )
            if f.rule_id not in config.disabled
        )
    if options.schema and not config.disabled >= check_ids("schema", enables_pass_only=True):
        findings.extend(
            _stage(
                "schema",
                lambda: validate_schema(
                    graph, options.schema_validator, allow_network=options.schema_allow_network
                ),
            )
        )
    if options.data and not config.disabled >= check_ids("data", enables_pass_only=True):
        findings.extend(
            f
            for f in _stage(
                "data",
                lambda: validate_data(
                    graph, options.data_validator, reader_factory=options.data_reader_factory
                ),
            )
            if f.rule_id not in config.disabled
        )
    if options.live and not config.disabled >= check_ids("live", enables_pass_only=True):
        findings.extend(
            f
            for f in _stage(
                "live",
                lambda: validate_live(
                    graph,
                    options.live_prober,
                    base_url=options.live_base_url,
                    known_statuses=known_statuses,
                ),
            )
            if f.rule_id not in config.disabled
        )
    return findings


def _stage(name: str, run: Callable[[], Iterable[Finding]]) -> list[Finding]:
    """Run one pass inside a span called ``name``, and log what it found."""
    log.debug("%s: start", name)
    with _trace.span(name):
        found = list(run())
    log.debug("%s: %d finding(s)", name, len(found))
    return found


def validate(
    catalog_path: Path | str,
    *,
    options: ValidationOptions | None = None,
) -> Report:
    """Validate a Portolan catalog tree, on disk or published over https.

    All settings below are fields of :class:`ValidationOptions`.

    ``catalog_path`` is the catalog directory, or the root ``catalog.json``
    inside it; both name the same catalog. It may instead be the https URL
    the catalog is published under — the root ``catalog.json`` or the
    directory holding it. rashid then fetches the root and follows its
    ``child``, ``item``, and JSON ``alternate`` links into a temporary tree
    (see :mod:`rashid.remote`), and runs the same passes over that. A tree
    assembled from links holds only what a link names, so the checks that
    look for unlinked files cannot see them; the report says so once, as a
    ``PTL-GEN-002`` warning. ``options.fetcher`` injects an alternate
    document fetcher, chiefly for testing. ``max_documents`` caps the crawl.

    The metadata pass always runs. The STAC 1.1.0 structural pass runs by
    default too, against the core schemas shipped in the wheel (see
    :mod:`rashid.structural`) — fully offline. ``structural=False`` skips it,
    as does disabling ``PTL-STR-001`` via ``config``.
    ``structural_validator`` injects an alternate validator, chiefly for
    testing.

    The extension pass runs by default too, validating every object against
    the schemas of the STAC extensions it declares (see
    :mod:`rashid.extensions`), from the schemas the profile's extension
    registry pins, shipped in the wheel. A registered extension declared at
    another version is validated against that version's schema, fetched over
    https. An extension the registry does not list is reported once as
    ``PTL-EXT-002`` rather than validated, unless ``schema_allow_network`` is
    set, which fetches it.
    ``extensions=False`` skips the pass, as does disabling ``PTL-EXT-001``
    via ``config``.

    When ``schema`` is true the Portolan profile schema pass runs too, applying
    the published JSON Schema to every object (see :mod:`rashid.schema`). The
    schema comes from the copies bundled in the wheel, so the pass is offline;
    it is off by default only because it overlaps the metadata pass by design.
    A schema version this build does not carry is fetched over the network when
    ``schema_allow_network`` is set, and degrades to a ``PTL-SCH-000`` warning
    otherwise. Disabling ``PTL-SCH-001`` via ``config`` skips the pass.
    ``schema_validator`` injects an alternate validator, chiefly for testing.

    The data pass runs by default too, reading each asset's bytes (local
    files and remote ``https`` URLs) to verify checksum, size, format, and
    spatial metadata (see :mod:`rashid.data`) — byte verification is the core
    of what a catalog validator is for. ``data=False`` skips it, worth doing
    when the assets are huge or remote and only the metadata verdict is
    needed. Disabling every ``PTL-DAT-00x`` rule via ``config`` also skips the
    pass; disabling a subset just silences those findings. ``data_validator``
    injects an alternate validator, chiefly for offline testing. If the
    geospatial stack cannot import (broken GDAL, wheel-less platform), the
    pass degrades to a single ``PTL-DAT-000`` warning.

    ``data_reader_factory`` narrows what the pass may read without turning it
    off: passing :class:`~rashid.data.reader.LocalOnlyReader` checks the assets
    that live in the tree and treats the rest as unfetchable, which is the
    difference between validating a metadata-only mirror and downloading the
    catalog it mirrors. It defaults to
    :class:`~rashid.data.reader.FilesystemHttpReader`, which reads both, and
    for a catalog read over https resolves the tree's relative hrefs under
    the catalog URL.

    When ``live`` is true the live-hosting pass runs too, probing the servers
    behind the catalog's assets for HTTP range support and CORS (see
    :mod:`rashid.live`) — absolute ``https`` hrefs as declared, relative hrefs
    when the publish base is known. That base is ``live_base_url`` (the https
    URL the catalog root is published under), else the catalog URL when
    ``catalog_path`` is one, else the root catalog's absolute ``self`` link.
    Given a base the pass also HEADs every link target under it, so a
    published tree missing the documents its ``child`` and ``item`` links name
    is reported (``PTL-LIV-006``), and compares the root ``self`` link against
    the base (``PTL-LIV-007``). With a catalog URL, ``live_base_url`` takes
    precedence over the URL if both are given; the CLI refuses that pair,
    since the URL is the base. ``live`` is off by default for a tree on disk
    because it reaches the network, and on by default for a catalog URL: the
    hosting MUSTs are what a published catalog is checked for, and the
    network is already in use. ``live=False`` turns it off either way.
    Disabling every ``PTL-LIV-00x`` rule via ``config`` skips the pass;
    disabling a subset just silences those findings. ``live_prober`` injects an
    alternate prober, chiefly for offline testing. Its ``head`` runs on several
    threads at once (see :class:`rashid.live.Prober`).
    """
    options = options or ValidationOptions()
    config = options.config or RulesConfig()
    url_mode = isinstance(catalog_path, str) and is_catalog_url(catalog_path)
    # Resolve the input-dependent default without mutating the caller's options.
    options = dataclasses.replace(
        options, live=(options.live is not False) if url_mode else bool(options.live)
    )
    if url_mode:
        return _validate_url(str(catalog_path), config, options)

    root = Path(catalog_path)

    # Shell completion lands on the file, and the root catalog.json names the
    # same catalog as the directory holding it. Any other file is refused:
    # a subcatalog's collection.json would otherwise validate its own
    # directory as a catalog root without saying so.
    if root.is_file() and root.name == ROOT_CATALOG.name:
        root = root.parent

    if not root.is_dir():
        return Report(
            findings=[
                Finding(
                    rule_id=GEN_MISSING_ROOT,
                    severity=Severity.ERROR,
                    message=(
                        f"catalog root is not a directory: {root}"
                        f" (pass the catalog directory or its {ROOT_CATALOG})"
                    ),
                    path=".",
                )
            ]
        )

    graph = _load(root)
    return _validate_graph(graph, config, options)


def _load(root: Path) -> CatalogGraph:
    with _trace.span("load"):
        graph = CatalogGraph.load(root)
    log.debug("load: %d object(s) under %s", len(graph.nodes), root)
    return graph


def _validate_url(
    url: str,
    config: RulesConfig,
    options: ValidationOptions,
) -> Report:
    """Fetch the published tree under ``url`` into a temporary directory and validate it."""
    base, root_url = split_catalog_url(url)
    with tempfile.TemporaryDirectory(prefix="rashid-") as tmp:
        try:
            with _trace.span("crawl"):
                crawled = crawl(
                    base, Path(tmp), options.fetcher, max_documents=options.max_documents
                )
        except FetchError as exc:
            return _missing_root(f"root catalog.json cannot be fetched: {exc}")
        status = crawled.statuses.get(root_url)
        if status is None:  # pragma: no cover - crawl raises FetchError for the root
            return _missing_root(f"root catalog.json cannot be fetched: GET {root_url} failed")
        if not 200 <= status < 300:
            return _missing_root(
                f"root catalog.json cannot be fetched: GET {root_url} returned {status}"
            )
        graph = _load(Path(tmp))
        graph.base_url = base
        return _validate_graph(
            graph,
            config,
            options,
            known_statuses=crawled.statuses,
            preamble=_partial_tree_findings(crawled, root_url),
        )


def _missing_root(detail: str) -> Report:
    return Report(
        findings=[
            Finding(
                rule_id=GEN_MISSING_ROOT,
                severity=Severity.ERROR,
                message=detail,
                path=str(ROOT_CATALOG),
            )
        ]
    )


def _partial_tree_findings(crawled: Crawl, root_url: str) -> list[Finding]:
    """What a tree assembled from links cannot show, said once at the root.

    A directory walk sees every file; a crawl sees what a link names. An
    object no link reaches is the fault ``PTL-LNK-002`` exists to report, and
    the scene files ``PTL-COL-005`` looks for beside a collection are never
    linked, so neither check can fire here. Reporting that once follows
    ``PTL-LIV-000``: a check that could not run is a warning, not a pass.
    """
    findings = [
        Finding(
            rule_id=GEN_PARTIAL_TREE,
            severity=Severity.WARNING,
            message=(
                f"catalog read by following links from {root_url}: {crawled.documents}"
                " document(s); a file no link names is invisible, so PTL-LNK-002 cannot"
                " report an unlinked object and PTL-COL-005 cannot see undeclared scene files"
            ),
            path=".",
            fix_hint="sync the tree to disk and run rashid check on the directory for the full view",
        )
    ]
    if crawled.capped:
        findings.append(
            Finding(
                rule_id=GEN_PARTIAL_TREE,
                severity=Severity.WARNING,
                message=(
                    f"crawl stopped at {crawled.documents} documents; the links past that"
                    " point were not followed"
                ),
                path=".",
                fix_hint="sync the tree to disk and run rashid check on the directory",
            )
        )
    for failed_url, error in sorted(crawled.errors.items()):
        findings.append(
            Finding(
                rule_id=GEN_PARTIAL_TREE,
                severity=Severity.WARNING,
                message=f"document could not be read: {failed_url}: {error}",
                path=str(ROOT_CATALOG),
                fix_hint=f"check that the host serves {failed_url} as a plain https document",
            )
        )
    return findings


def _run_rules(graph: CatalogGraph, rules: Sequence[Rule], config: RulesConfig) -> list[Finding]:
    """Every enabled rule over every object of the kinds it reads.

    The node-check counter is added once at the end, not once per check, so
    the count costs nothing inside the loop.
    """
    findings: list[Finding] = []
    checks = 0
    for rule in rules:
        if rule.id in config.disabled:
            continue
        if not rule.kinds:
            checks += 1
            findings.extend(rule.check_graph(graph))
            continue
        for node in graph.iter(*rule.kinds):
            if node.parse_error is not None:
                continue
            checks += 1
            findings.extend(rule.check(node, graph))
    _trace.count("rules.checks", checks)
    return findings


def _validate_graph(
    graph: CatalogGraph,
    config: RulesConfig,
    options: ValidationOptions,
    *,
    preamble: Sequence[Finding] = (),
    known_statuses: Mapping[str, int] | None = None,
) -> Report:
    """Run every rule and every requested pass over a loaded graph."""
    findings: list[Finding] = list(preamble)

    root_node = graph.nodes.get(ROOT_CATALOG)
    if root_node is None or graph.root is None:
        detail = (
            f"root catalog.json cannot be parsed: {root_node.parse_error}"
            if root_node is not None and root_node.parse_error
            else "root catalog.json is missing or is not a STAC Catalog"
        )
        return Report(
            findings=[
                Finding(
                    rule_id=GEN_MISSING_ROOT,
                    severity=Severity.ERROR,
                    message=detail,
                    path=str(ROOT_CATALOG),
                )
            ],
            files_checked=len(graph.nodes),
        )

    findings.extend(
        Finding(
            rule_id=GEN_UNPARSEABLE,
            severity=Severity.ERROR,
            message=f"file is not valid JSON: {node.parse_error}",
            path=str(node.path),
        )
        for node in graph.iter()
        if node.parse_error is not None
    )

    rules = options.rules
    if rules is None:
        rules = DEFAULT_RULES

    findings.extend(_stage("rules", lambda: _run_rules(graph, rules, config)))

    findings.extend(_optional_passes(graph, config, options, known_statuses=known_statuses))

    if config.severity_overrides:
        findings = [
            dataclasses.replace(f, severity=config.severity_overrides[f.rule_id])
            if f.rule_id in config.severity_overrides
            else f
            for f in findings
        ]

    findings.sort(key=lambda f: (f.path, f.rule_id, f.message))
    return Report(findings=findings, files_checked=len(graph.nodes))
