"""Structural pass tests.

Deterministic tests inject a fake ``Validator``; the real validator runs
offline against the vendored STAC 1.1.0 closure, so it is exercised directly
too — including the builder's whole tree, which must be structurally valid.
"""

from __future__ import annotations

import pytest

from rashid import RulesConfig, validate, validate_structural
from rashid._jsonschema import SchemaError
from rashid.catalog import CatalogGraph
from rashid.model import Severity
from rashid.structural import default_validator
from tests.conftest import CatalogBuilder, mutate_json

pytestmark = pytest.mark.unit


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    return CatalogGraph.load(catalog.write())


def _error(message: str, pointer: str = "") -> SchemaError:
    return SchemaError(message=message, json_pointer=pointer)


def test_all_valid_yields_no_findings(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    assert validate_structural(_graph(catalog), lambda data: []) == []


def test_invalid_object_becomes_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    graph = _graph(catalog)

    def check(data: dict) -> list[SchemaError]:
        if data.get("type") == "Collection":
            return [_error("'extent' is a required property")]
        return []

    findings = validate_structural(graph, check)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-STR-001"
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert "'extent' is a required property" in findings[0].message


def test_findings_carry_the_json_pointer(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    graph = _graph(catalog)

    def check(data: dict) -> list[SchemaError]:
        if data.get("type") == "Collection":
            return [_error("bad link type", "/links/0/type")]
        return []

    (finding,) = validate_structural(graph, check)
    assert finding.json_pointer == "/links/0/type"


def test_validator_failure_is_a_single_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("a")
    catalog.collection("b")
    graph = _graph(catalog)

    def boom(data: dict) -> list[SchemaError]:
        raise RuntimeError("registry resolution failed")

    findings = validate_structural(graph, boom)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-STR-000"
    assert findings[0].severity is Severity.WARNING
    assert "registry resolution failed" in findings[0].message


def test_missing_package_is_a_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rashid.structural as structural

    def raise_import() -> None:
        raise ImportError("No module named 'jsonschema'")

    monkeypatch.setattr(structural, "default_validator", raise_import)
    findings = structural.validate_structural(_graph(catalog), None)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-STR-000"
    assert "not installed" in findings[0].message


def test_default_validator_used_when_none(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rashid.structural as structural

    monkeypatch.setattr(structural, "default_validator", lambda: lambda data: [])
    assert structural.validate_structural(_graph(catalog), None) == []


def test_unparseable_object_is_skipped(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "collection.json").write_text("{ broken", encoding="utf-8")
    graph = CatalogGraph.load(root)

    seen: list[str | None] = []

    def check(data: dict) -> list[SchemaError]:
        seen.append(data.get("type"))
        return []

    assert validate_structural(graph, check) == []
    assert "Collection" not in seen  # the broken collection never reached the validator


def test_real_validator_accepts_the_builder(catalog: CatalogBuilder) -> None:
    """The builder's whole tree is valid STAC 1.1.0, verdict from the vendored closure."""
    catalog.collection("roads")
    report = validate(catalog.write())
    assert [f for f in report.findings if f.rule_id == "PTL-STR-001"] == []


def test_real_validator_rejects_a_broken_collection(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("extent"))

    report = validate(root)
    structural = [f for f in report.findings if f.rule_id == "PTL-STR-001"]
    assert structural, "the vendored STAC schema accepted a collection without extent"
    assert any("extent" in f.message for f in structural)
    assert all(f.json_pointer is not None for f in structural)


def test_real_validator_rejects_an_unknown_type() -> None:
    validate_object = default_validator()
    (error,) = validate_object({"type": "Banana"})
    assert error.json_pointer == "/type"
    assert "Banana" in error.message


def test_real_validator_validates_items(catalog: CatalogBuilder) -> None:
    """Items route to item.json, whose GeoJSON refs must resolve offline."""
    collection = catalog.collection("roads")
    collection.item("scene-1")
    report = validate(catalog.write())
    assert [f for f in report.findings if f.rule_id == "PTL-STR-001"] == []


def test_runner_wires_structural_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def check(data: dict) -> list[SchemaError]:
        return [_error("bad root")] if data.get("type") == "Catalog" else []

    report = validate(root, structural=True, structural_validator=check)
    assert not report.passed
    assert any(f.rule_id == "PTL-STR-001" for f in report.findings)


def test_structural_on_by_default(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    report = validate(root, structural_validator=lambda data: [_error("seen")])
    assert any(f.rule_id == "PTL-STR-001" for f in report.findings)


def test_structural_can_be_disabled(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    report = validate(root, structural=False, structural_validator=lambda data: [_error("never")])
    assert all(f.rule_id != "PTL-STR-001" for f in report.findings)


def test_disabling_str_rule_skips_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    calls: list[dict] = []

    def check(data: dict) -> list[SchemaError]:
        calls.append(data)
        return [_error("bad")]

    report = validate(
        root,
        config=RulesConfig(disabled=frozenset({"PTL-STR-001"})),
        structural=True,
        structural_validator=check,
    )
    assert all(f.rule_id != "PTL-STR-001" for f in report.findings)
    assert calls == []  # the pass was skipped entirely, validator never invoked
