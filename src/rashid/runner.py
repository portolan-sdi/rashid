"""Validation runner: build the graph, run the rules, produce a report."""

from __future__ import annotations

import dataclasses
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from rashid._jsonschema import SchemaError
from rashid.catalog import ROOT_CATALOG, CatalogGraph
from rashid.config import RulesConfig
from rashid.data import (
    DAT_CHECKSUM,
    DAT_COG,
    DAT_COG_STATS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_GEOPARQUET_VERSION,
    DAT_MIRROR,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SIZE,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
    DAT_VECTOR_COLUMNS,
    validate_data,
)
from rashid.data import ReaderFactory as DataReaderFactory
from rashid.data import Validator as DataValidator
from rashid.live import (
    LIV_CORS_EXPOSE,
    LIV_CORS_ORIGIN,
    LIV_CORS_PREFLIGHT,
    LIV_HEAD_LENGTH,
    LIV_LINK_TARGET,
    LIV_RANGE,
    LIV_SELF_BASE,
    validate_live,
)
from rashid.live import Prober as LiveProber
from rashid.model import Finding, Report, Severity
from rashid.remote import (
    DEFAULT_MAX_DOCUMENTS,
    Crawl,
    Fetcher,
    FetchError,
    crawl,
    is_catalog_url,
    split_catalog_url,
)
from rashid.rule import Rule
from rashid.schema import SCH_INVALID, validate_schema
from rashid.structural import STR_INVALID, validate_structural

GEN_MISSING_ROOT = "PTL-GEN-000"
GEN_UNPARSEABLE = "PTL-GEN-001"
GEN_PARTIAL_TREE = "PTL-GEN-002"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
SPEC_IDS: dict[str, tuple[str, ...]] = {
    # A missing/invalid root catalog.json breaks the always-required
    # entrypoint; an unparseable object cannot be a valid STAC catalog.
    GEN_MISSING_ROOT: ("PORTO-CORE-001", "PORTO-CORE-013"),
    GEN_UNPARSEABLE: ("PORTO-CORE-001",),
}

# Every rule the data pass can raise; disabling all of them skips the (networked)
# pass entirely, while disabling any subset just silences those findings. An id
# left out is a rule that vanishes when the listed ones are disabled, without
# anyone having disabled it, so tests/unit/test_runner_data_rules.py derives the
# membership from the registry rather than trusting this list.
_DATA_RULE_IDS = frozenset(
    {
        DAT_CHECKSUM,
        DAT_SIZE,
        DAT_FORMAT,
        DAT_COG,
        DAT_CONSISTENCY,
        DAT_ORDERING,
        DAT_ROWGROUP_STATS,
        DAT_ROWGROUP_SIZE,
        DAT_COG_STATS,
        DAT_VALID_PERCENT,
        DAT_OVERVIEWS,
        DAT_GEOPARQUET_VERSION,
        DAT_TILE_SIZE,
        DAT_PARTITION_SCHEMA,
        DAT_TABULAR,
        DAT_MIRROR,
        DAT_VECTOR_COLUMNS,
    }
)

# Every rule the live pass can raise; disabling all of them skips the (networked)
# pass entirely, while disabling any subset just silences those findings.
_LIVE_RULE_IDS = frozenset(
    {
        LIV_RANGE,
        LIV_HEAD_LENGTH,
        LIV_CORS_ORIGIN,
        LIV_CORS_EXPOSE,
        LIV_CORS_PREFLIGHT,
        LIV_LINK_TARGET,
        LIV_SELF_BASE,
    }
)

# Structural/schema validators map one object's raw JSON to schema errors.
_Validator = Callable[[dict[str, Any]], list[SchemaError]]


@dataclasses.dataclass(frozen=True)
class _PassOptions:
    """The caller's choices for the opt-in passes, carried as one value."""

    structural: bool
    structural_validator: _Validator | None
    schema: bool
    schema_validator: _Validator | None
    schema_allow_network: bool
    data: bool
    data_validator: DataValidator | None
    data_reader_factory: DataReaderFactory | None
    live: bool
    live_prober: LiveProber | None
    live_base_url: str | None
    live_known_statuses: Mapping[str, int] | None = None


def _optional_passes(
    graph: CatalogGraph, config: RulesConfig, options: _PassOptions
) -> list[Finding]:
    """Run the opt-in structural, schema, data, and live passes, honouring disable ids."""
    findings: list[Finding] = []
    if options.structural and STR_INVALID not in config.disabled:
        findings.extend(validate_structural(graph, options.structural_validator))
    if options.schema and SCH_INVALID not in config.disabled:
        findings.extend(
            validate_schema(
                graph, options.schema_validator, allow_network=options.schema_allow_network
            )
        )
    if options.data and not _DATA_RULE_IDS <= config.disabled:
        findings.extend(
            f
            for f in validate_data(
                graph, options.data_validator, reader_factory=options.data_reader_factory
            )
            if f.rule_id not in config.disabled
        )
    if options.live and not _LIVE_RULE_IDS <= config.disabled:
        findings.extend(
            f
            for f in validate_live(
                graph,
                options.live_prober,
                base_url=options.live_base_url,
                known_statuses=options.live_known_statuses,
            )
            if f.rule_id not in config.disabled
        )
    return findings


def validate(
    catalog_path: Path | str,
    rules: Sequence[Rule] | None = None,
    config: RulesConfig | None = None,
    *,
    structural: bool = True,
    structural_validator: Callable[[dict[str, Any]], list[SchemaError]] | None = None,
    schema: bool = False,
    schema_validator: Callable[[dict[str, Any]], list[SchemaError]] | None = None,
    schema_allow_network: bool = False,
    data: bool = True,
    data_validator: DataValidator | None = None,
    data_reader_factory: DataReaderFactory | None = None,
    live: bool | None = None,
    live_prober: LiveProber | None = None,
    live_base_url: str | None = None,
    fetcher: Fetcher | None = None,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
) -> Report:
    """Validate a Portolan catalog tree, on disk or published over https.

    ``catalog_path`` is the catalog directory, or the root ``catalog.json``
    inside it; both name the same catalog. It may instead be the https URL
    the catalog is published under — the root ``catalog.json`` or the
    directory holding it. rashid then fetches the root and follows its
    ``child``, ``item``, and JSON ``alternate`` links into a temporary tree
    (see :mod:`rashid.remote`), and runs the same passes over that. A tree
    assembled from links holds only what a link names, so the checks that
    look for unlinked files cannot see them; the report says so once, as a
    ``PTL-GEN-002`` warning. ``fetcher`` injects an alternate document
    fetcher and ``max_documents`` caps the crawl; both matter chiefly for
    testing.

    The metadata pass always runs. The STAC 1.1.0 structural pass runs by
    default too, against the core schemas shipped in the wheel (see
    :mod:`rashid.structural`) — fully offline. ``structural=False`` skips it,
    as does disabling ``PTL-STR-001`` via ``config``.
    ``structural_validator`` injects an alternate validator, chiefly for
    testing.

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
    the base (``PTL-LIV-007``). ``live`` is off by default for a tree on disk
    because it reaches the network, and on by default for a catalog URL: the
    hosting MUSTs are what a published catalog is checked for, and the
    network is already in use. ``live=False`` turns it off either way.
    Disabling every ``PTL-LIV-00x`` rule via ``config`` skips the pass;
    disabling a subset just silences those findings. ``live_prober`` injects an
    alternate prober, chiefly for offline testing.
    """
    if rules is None:
        from rashid.rules import DEFAULT_RULES

        rules = DEFAULT_RULES
    config = config or RulesConfig()
    url_mode = isinstance(catalog_path, str) and is_catalog_url(catalog_path)
    options = _PassOptions(
        structural=structural,
        structural_validator=structural_validator,
        schema=schema,
        schema_validator=schema_validator,
        schema_allow_network=schema_allow_network,
        data=data,
        data_validator=data_validator,
        data_reader_factory=data_reader_factory,
        # Off by default on disk, on by default for a URL: see the docstring.
        live=(live is not False) if url_mode else bool(live),
        live_prober=live_prober,
        live_base_url=live_base_url,
    )

    if url_mode:
        return _validate_url(
            str(catalog_path), rules, config, options, fetcher=fetcher, max_documents=max_documents
        )

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

    graph = CatalogGraph.load(root)
    return _validate_graph(graph, rules, config, options)


def _validate_url(
    url: str,
    rules: Sequence[Rule],
    config: RulesConfig,
    options: _PassOptions,
    *,
    fetcher: Fetcher | None,
    max_documents: int,
) -> Report:
    """Fetch the published tree under ``url`` into a temporary directory and validate it."""
    base, root_url = split_catalog_url(url)
    with tempfile.TemporaryDirectory(prefix="rashid-") as tmp:
        try:
            crawled = crawl(base, Path(tmp), fetcher, max_documents=max_documents)
        except FetchError as exc:
            return _missing_root(f"root catalog.json cannot be fetched: {exc}")
        status = crawled.statuses.get(root_url)
        if status is None or not 200 <= status < 300:
            return _missing_root(
                f"root catalog.json cannot be fetched: GET {root_url} returned {status}"
            )
        graph = CatalogGraph.load(Path(tmp))
        graph.base_url = base
        graph.complete_listing = False
        return _validate_graph(
            graph,
            rules,
            config,
            dataclasses.replace(options, live_known_statuses=crawled.statuses),
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
                message=f"document could not be fetched: {error}",
                path=str(ROOT_CATALOG),
                fix_hint=f"check that the host serves {failed_url}",
            )
        )
    return findings


def _validate_graph(
    graph: CatalogGraph,
    rules: Sequence[Rule],
    config: RulesConfig,
    options: _PassOptions,
    *,
    preamble: Sequence[Finding] = (),
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

    for node in graph.iter():
        if node.parse_error is not None:
            findings.append(
                Finding(
                    rule_id=GEN_UNPARSEABLE,
                    severity=Severity.ERROR,
                    message=f"file is not valid JSON: {node.parse_error}",
                    path=str(node.path),
                )
            )

    for rule in rules:
        if rule.id in config.disabled:
            continue
        if not rule.kinds:
            findings.extend(rule.check_graph(graph))
            continue
        for node in graph.iter(*rule.kinds):
            if node.parse_error is not None:
                continue
            findings.extend(rule.check(node, graph))

    findings.extend(_optional_passes(graph, config, options))

    if config.severity_overrides:
        findings = [
            dataclasses.replace(f, severity=config.severity_overrides[f.rule_id])
            if f.rule_id in config.severity_overrides
            else f
            for f in findings
        ]

    findings.sort(key=lambda f: (f.path, f.rule_id, f.message))
    return Report(findings=findings, files_checked=len(graph.nodes))
