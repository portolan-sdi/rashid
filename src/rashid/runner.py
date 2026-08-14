"""Validation runner: build the graph, run the rules, produce a report."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
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
    LIV_RANGE,
    validate_live,
)
from rashid.live import Prober as LiveProber
from rashid.model import Finding, Report, Severity
from rashid.rule import Rule
from rashid.schema import SCH_INVALID, validate_schema
from rashid.structural import STR_INVALID, validate_structural

GEN_MISSING_ROOT = "PTL-GEN-000"
GEN_UNPARSEABLE = "PTL-GEN-001"

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
    }
)

# Structural/schema validators map one object's raw JSON to schema errors.
_Validator = Callable[[dict[str, Any]], list[SchemaError]]


def _optional_passes(
    graph: CatalogGraph,
    config: RulesConfig,
    *,
    structural: bool,
    structural_validator: _Validator | None,
    schema: bool,
    schema_validator: _Validator | None,
    schema_allow_network: bool,
    data: bool,
    data_validator: DataValidator | None,
    data_reader_factory: DataReaderFactory | None,
    live: bool,
    live_prober: LiveProber | None,
    live_base_url: str | None,
) -> list[Finding]:
    """Run the opt-in structural, schema, data, and live passes, honouring disable ids."""
    findings: list[Finding] = []
    if structural and STR_INVALID not in config.disabled:
        findings.extend(validate_structural(graph, structural_validator))
    if schema and SCH_INVALID not in config.disabled:
        findings.extend(
            validate_schema(graph, schema_validator, allow_network=schema_allow_network)
        )
    if data and not _DATA_RULE_IDS <= config.disabled:
        findings.extend(
            f
            for f in validate_data(graph, data_validator, reader_factory=data_reader_factory)
            if f.rule_id not in config.disabled
        )
    if live and not _LIVE_RULE_IDS <= config.disabled:
        findings.extend(
            f
            for f in validate_live(graph, live_prober, base_url=live_base_url)
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
    live: bool = False,
    live_prober: LiveProber | None = None,
    live_base_url: str | None = None,
) -> Report:
    """Validate a local Portolan catalog tree.

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
    :class:`~rashid.data.reader.FilesystemHttpReader`, which reads both.

    When ``live`` is true the live-hosting pass runs too, probing the servers
    behind the catalog's assets for HTTP range support and CORS (see
    :mod:`rashid.live`) — absolute ``https`` hrefs as declared, relative hrefs
    when ``live_base_url`` (the https URL the catalog root is published under)
    is given; it is off by default because it reaches the network.
    Disabling every ``PTL-LIV-00x`` rule via ``config`` skips the pass;
    disabling a subset just silences those findings. ``live_prober`` injects an
    alternate prober, chiefly for offline testing.
    """
    if rules is None:
        from rashid.rules import DEFAULT_RULES

        rules = DEFAULT_RULES
    config = config or RulesConfig()
    root = Path(catalog_path)

    if not root.is_dir():
        return Report(
            findings=[
                Finding(
                    rule_id=GEN_MISSING_ROOT,
                    severity=Severity.ERROR,
                    message=f"catalog root is not a directory: {root}",
                    path=".",
                )
            ]
        )

    graph = CatalogGraph.load(root)
    findings: list[Finding] = []

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

    findings.extend(
        _optional_passes(
            graph,
            config,
            structural=structural,
            structural_validator=structural_validator,
            schema=schema,
            schema_validator=schema_validator,
            schema_allow_network=schema_allow_network,
            data=data,
            data_validator=data_validator,
            data_reader_factory=data_reader_factory,
            live=live,
            live_prober=live_prober,
            live_base_url=live_base_url,
        )
    )

    if config.severity_overrides:
        findings = [
            dataclasses.replace(f, severity=config.severity_overrides[f.rule_id])
            if f.rule_id in config.severity_overrides
            else f
            for f in findings
        ]

    findings.sort(key=lambda f: (f.path, f.rule_id, f.message))
    return Report(findings=findings, files_checked=len(graph.nodes))
