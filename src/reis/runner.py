"""Validation runner: build the graph, run the rules, produce a report."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from reis.catalog import ROOT_CATALOG, CatalogGraph
from reis.config import RulesConfig
from reis.data import (
    DAT_CHECKSUM,
    DAT_COG,
    DAT_COG_STATS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_GEOPARQUET_VERSION,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SIZE,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
    validate_data,
)
from reis.data import Validator as DataValidator
from reis.live import (
    LIV_CORS_EXPOSE,
    LIV_CORS_ORIGIN,
    LIV_CORS_PREFLIGHT,
    LIV_HEAD_LENGTH,
    LIV_RANGE,
    validate_live,
)
from reis.live import Prober as LiveProber
from reis.model import Finding, Report, Severity
from reis.rule import Rule
from reis.schema import SCH_INVALID, validate_schema
from reis.structural import STR_INVALID, validate_structural

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
# pass entirely, while disabling any subset just silences those findings.
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

_Validator = Callable[[dict[str, Any]], list[str]]


def _optional_passes(
    graph: CatalogGraph,
    config: RulesConfig,
    *,
    structural: bool,
    structural_validator: _Validator | None,
    schema: bool,
    schema_validator: _Validator | None,
    data: bool,
    data_validator: DataValidator | None,
    live: bool,
    live_prober: LiveProber | None,
) -> list[Finding]:
    """Run the opt-in structural, schema, data, and live passes, honouring disable ids."""
    findings: list[Finding] = []
    if structural and STR_INVALID not in config.disabled:
        findings.extend(validate_structural(graph, structural_validator))
    if schema and SCH_INVALID not in config.disabled:
        findings.extend(validate_schema(graph, schema_validator))
    if data and not _DATA_RULE_IDS <= config.disabled:
        findings.extend(
            f for f in validate_data(graph, data_validator) if f.rule_id not in config.disabled
        )
    if live and not _LIVE_RULE_IDS <= config.disabled:
        findings.extend(
            f for f in validate_live(graph, live_prober) if f.rule_id not in config.disabled
        )
    return findings


def validate(
    catalog_path: Path | str,
    rules: Sequence[Rule] | None = None,
    config: RulesConfig | None = None,
    *,
    structural: bool = False,
    structural_validator: Callable[[dict[str, Any]], list[str]] | None = None,
    schema: bool = False,
    schema_validator: Callable[[dict[str, Any]], list[str]] | None = None,
    data: bool = False,
    data_validator: DataValidator | None = None,
    live: bool = False,
    live_prober: LiveProber | None = None,
) -> Report:
    """Validate a local Portolan catalog tree.

    The metadata pass always runs. When ``structural`` is true the STAC 1.1.0
    structural pass runs too, delegated to stac-validator (see
    :mod:`reis.structural`); it is off by default here because it reaches the
    network, and on by default in the CLI. Disabling ``PTL-STR-001`` via
    ``config`` skips the structural pass. ``structural_validator`` injects an
    alternate validator, chiefly for offline testing.

    When ``schema`` is true the Portolan profile schema pass runs too, applying
    the published JSON Schema to every object (see :mod:`reis.schema`); it is
    off by default because it reaches the network and overlaps the metadata
    pass. Disabling ``PTL-SCH-001`` via ``config`` skips it. ``schema_validator``
    injects an alternate validator, chiefly for offline testing.

    When ``data`` is true the data pass runs too, reading each asset's bytes
    (local files and remote ``https`` URLs) to verify checksum, size, format,
    and spatial metadata (see :mod:`reis.data`); it is off by default because it
    reaches the network and needs the ``reis[data]`` extra. Disabling every
    ``PTL-DAT-00x`` rule via ``config`` skips the pass; disabling a subset just
    silences those findings. ``data_validator`` injects an alternate validator,
    chiefly for offline testing.

    When ``live`` is true the live-hosting pass runs too, probing the servers
    behind absolute ``https`` asset hrefs for HTTP range support and CORS (see
    :mod:`reis.live`); it is off by default because it reaches the network.
    Disabling every ``PTL-LIV-00x`` rule via ``config`` skips the pass;
    disabling a subset just silences those findings. ``live_prober`` injects an
    alternate prober, chiefly for offline testing.
    """
    if rules is None:
        from reis.rules import DEFAULT_RULES

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
            data=data,
            data_validator=data_validator,
            live=live,
            live_prober=live_prober,
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
