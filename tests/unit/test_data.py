"""Unit tests for the data pass wiring — validator injection, no bytes, no deps.

These exercise :func:`reis.data.validate_data` and its runner/CLI wiring with a
fake validator, exactly as ``test_schema.py`` does for the schema pass: the
byte-reading and geospatial stack are not touched here (that is
``test_data_checks``/``test_data_catalog``), so these run without the
``reis[data]`` extra.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reis.data as data_pass
from reis.catalog import CatalogGraph, Node
from reis.config import RulesConfig
from reis.data import (
    DAT_CHECKSUM,
    DAT_COG,
    DAT_COG_STATS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SIZE,
    DAT_UNAVAILABLE,
    DAT_VALID_PERCENT,
    DataDefect,
    validate_data,
)
from reis.data.reader import AssetReader
from reis.model import Severity
from reis.runner import validate
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    catalog.collection("roads").item("seg1")
    return CatalogGraph.load(catalog.write())


def _defect_on_items(
    rule_id: str = DAT_CHECKSUM, field: str | None = "file:checksum"
) -> data_pass.Validator:
    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "item":
            return []
        return [DataDefect(rule_id, Severity.ERROR, "asset 'data' bytes diverge", "data", field)]

    return check


def test_defect_maps_to_finding(catalog: CatalogBuilder) -> None:
    graph = _graph(catalog)

    findings = validate_data(graph, _defect_on_items())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == DAT_CHECKSUM
    assert finding.severity is Severity.ERROR
    assert finding.object_id == "seg1"
    assert finding.json_pointer == "/assets/data/file:checksum"
    assert finding.path.endswith("seg1.json")


def test_defect_without_field_points_at_asset(catalog: CatalogBuilder) -> None:
    findings = validate_data(_graph(catalog), _defect_on_items(DAT_CONSISTENCY, field=None))

    assert findings[0].json_pointer == "/assets/data"


def test_no_defects_is_clean(catalog: CatalogBuilder) -> None:
    def clean(node: Node, reader: AssetReader) -> list[DataDefect]:
        return []

    assert validate_data(_graph(catalog), clean) == []


def test_systemic_failure_reported_once(catalog: CatalogBuilder) -> None:
    def boom(node: Node, reader: AssetReader) -> list[DataDefect]:
        raise RuntimeError("reader exploded")

    findings = validate_data(_graph(catalog), boom)

    assert len(findings) == 1
    assert findings[0].rule_id == DAT_UNAVAILABLE
    assert findings[0].severity is Severity.WARNING
    assert "reader exploded" in findings[0].message


def test_missing_extra_downgrades_to_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_import() -> data_pass.Validator:
        raise ImportError("No module named 'pyarrow'")

    monkeypatch.setattr(data_pass, "default_validator", raise_import)

    findings = validate_data(_graph(catalog))

    assert len(findings) == 1
    assert findings[0].rule_id == DAT_UNAVAILABLE
    assert "reis[data]" in findings[0].message


def test_data_off_by_default(catalog: CatalogBuilder) -> None:
    root = _graph(catalog).root_path
    report = validate(root)
    assert not any(f.rule_id.startswith("PTL-DAT") for f in report.findings)


def test_data_pass_surfaces_with_flag(catalog: CatalogBuilder) -> None:
    root = _graph(catalog).root_path
    report = validate(root, data=True, data_validator=_defect_on_items())
    assert any(f.rule_id == DAT_CHECKSUM for f in report.findings)


def test_disabling_one_rule_silences_only_it(catalog: CatalogBuilder) -> None:
    graph_root = _graph(catalog).root_path

    def two_defects(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "item":
            return []
        return [
            DataDefect(DAT_CHECKSUM, Severity.ERROR, "checksum", "data", "file:checksum"),
            DataDefect(DAT_SIZE, Severity.ERROR, "size", "data", "file:size"),
        ]

    report = validate(
        graph_root,
        config=RulesConfig(disabled=frozenset({DAT_SIZE})),
        data=True,
        data_validator=two_defects,
    )
    ids = {f.rule_id for f in report.findings}
    assert DAT_CHECKSUM in ids
    assert DAT_SIZE not in ids


def test_disabling_all_rules_skips_pass(catalog: CatalogBuilder) -> None:
    graph_root = _graph(catalog).root_path
    called = False

    def spy(node: Node, reader: AssetReader) -> list[DataDefect]:
        nonlocal called
        called = True
        return []

    all_ids = frozenset(
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
        }
    )
    validate(graph_root, config=RulesConfig(disabled=all_ids), data=True, data_validator=spy)

    assert called is False


def test_parse_error_nodes_are_skipped(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _graph(catalog).root_path
    # Corrupt the collection JSON so its node carries a parse_error.
    (root / "roads" / "collection.json").write_text("{ not json", encoding="utf-8")
    graph = CatalogGraph.load(root)

    seen: list[str] = []

    def record(node: Node, reader: AssetReader) -> list[DataDefect]:
        seen.append(node.kind)
        return []

    validate_data(graph, record)
    assert "unknown" not in seen  # the unparseable collection is not handed to the validator


def test_defect_with_explicit_pointer_keeps_it(catalog: CatalogBuilder) -> None:
    # Node-level defects (the partition-schema check) bind to no asset and
    # carry their own pointer instead of the /assets/<key> construction.
    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "collection":
            return []
        return [
            DataDefect(
                DAT_PARTITION_SCHEMA,
                Severity.ERROR,
                "partition schemas diverge",
                "",
                json_pointer="/partition:glob",
            )
        ]

    findings = validate_data(_graph(catalog), check)

    assert len(findings) == 1
    assert findings[0].rule_id == DAT_PARTITION_SCHEMA
    assert findings[0].json_pointer == "/partition:glob"
