"""Structural pass tests.

The default validator reaches the network, so every deterministic test injects
a fake ``Validator`` (or monkeypatches ``default_validator``); one self-skipping
test exercises the real stac-validator when schemas.stacspec.org is reachable.
"""

from __future__ import annotations

import pytest

from reis import RulesConfig, validate, validate_structural
from reis.catalog import CatalogGraph
from reis.model import Severity
from reis.structural import default_validator
from tests.conftest import CatalogBuilder, skip_if_network_flaked

pytestmark = pytest.mark.unit


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    return CatalogGraph.load(catalog.write())


def test_all_valid_yields_no_findings(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    assert validate_structural(_graph(catalog), lambda data: []) == []


def test_invalid_object_becomes_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    graph = _graph(catalog)

    def check(data: dict) -> list[str]:
        return ["'extent' is a required property"] if data.get("type") == "Collection" else []

    findings = validate_structural(graph, check)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-STR-001"
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert "'extent' is a required property" in findings[0].message


def test_validator_failure_is_a_single_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("a")
    catalog.collection("b")
    graph = _graph(catalog)

    def boom(data: dict) -> list[str]:
        raise RuntimeError("could not reach schemas.stacspec.org")

    findings = validate_structural(graph, boom)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-STR-000"
    assert findings[0].severity is Severity.WARNING
    assert "could not reach" in findings[0].message


def test_missing_package_is_a_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reis.structural as structural

    def raise_import() -> None:
        raise ImportError("No module named 'stac_validator'")

    monkeypatch.setattr(structural, "default_validator", raise_import)
    findings = structural.validate_structural(_graph(catalog), None)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-STR-000"
    assert "not installed" in findings[0].message


def test_default_validator_used_when_none(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reis.structural as structural

    monkeypatch.setattr(structural, "default_validator", lambda: lambda data: [])
    assert structural.validate_structural(_graph(catalog), None) == []


def test_unparseable_object_is_skipped(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "collection.json").write_text("{ broken", encoding="utf-8")
    graph = CatalogGraph.load(root)

    seen: list[str | None] = []

    def check(data: dict) -> list[str]:
        seen.append(data.get("type"))
        return []

    assert validate_structural(graph, check) == []
    assert "Collection" not in seen  # the broken collection never reached the validator


def test_default_validator_parses_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    from stac_validator import stac_validator

    class FakeStacValidate:
        def __init__(self, **kwargs: object) -> None:
            self.options = kwargs  # StacValidate(core=True) — accepted, unused
            self.message: list[dict] = []

        def validate_dict(self, data: dict) -> bool:
            self.message = data["_messages"]
            return all(m.get("valid_stac") for m in self.message)

    monkeypatch.setattr(stac_validator, "StacValidate", FakeStacValidate)
    validate_object = default_validator()

    assert validate_object({"_messages": [{"valid_stac": True}]}) == []
    assert validate_object(
        {
            "_messages": [
                {"valid_stac": False, "error_message": "'x' required", "failed_schema": "S"}
            ]
        }
    ) == ["'x' required (schema: S)"]
    assert validate_object({"_messages": [{"valid_stac": False, "error_type": "TypeError"}]}) == [
        "TypeError"
    ]
    assert validate_object({"_messages": [{"valid_stac": False}]}) == [
        "object is not structurally valid STAC"
    ]


def test_runner_wires_structural_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def check(data: dict) -> list[str]:
        return ["bad root"] if data.get("type") == "Catalog" else []

    report = validate(root, structural=True, structural_validator=check)
    assert not report.passed
    assert any(f.rule_id == "PTL-STR-001" for f in report.findings)


def test_structural_off_by_default(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    report = validate(root, structural_validator=lambda data: ["never seen"])
    assert all(f.rule_id != "PTL-STR-001" for f in report.findings)


def test_disabling_str_rule_skips_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    calls: list[dict] = []

    def check(data: dict) -> list[str]:
        calls.append(data)
        return ["bad"]

    report = validate(
        root,
        config=RulesConfig(disabled=frozenset({"PTL-STR-001"})),
        structural=True,
        structural_validator=check,
    )
    assert all(f.rule_id != "PTL-STR-001" for f in report.findings)
    assert calls == []  # the pass was skipped entirely, validator never invoked


@pytest.mark.network
def test_real_stac_validator_accepts_the_builder(catalog: CatalogBuilder) -> None:
    import urllib.request

    try:
        urllib.request.urlopen("https://schemas.stacspec.org/", timeout=5)  # noqa: S310
    except Exception:  # noqa: BLE001
        pytest.skip("schemas.stacspec.org unreachable")

    catalog.collection("roads")
    report = validate(catalog.write(), structural=True)
    structural_errors = [f for f in report.findings if f.rule_id == "PTL-STR-001"]
    # The validator resolves remote schemas, so a reset connection reads as an
    # unresolvable reference rather than a conformance fault.
    skip_if_network_flaked(f.message for f in structural_errors)
    assert structural_errors == []  # the builder's tree is valid STAC 1.1.0
