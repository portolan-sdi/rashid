"""Schema pass tests.

The default validator reaches the network, so every deterministic test injects
a fake ``Validator`` (or monkeypatches ``default_validator``); one self-skipping
test exercises the real published schema when schemas.portolan-sdi.org is
reachable.
"""

from __future__ import annotations

import pytest

from rashid import RulesConfig, validate, validate_schema
from rashid.catalog import CatalogGraph
from rashid.model import Severity
from rashid.schema import DEFAULT_SCHEMA_URI, _schema_uri_for, default_validator
from tests.conftest import (
    PORTOLAN_URI,
    CatalogBuilder,
    mutate_json,
    skip_if_network_flaked,
)

pytestmark = pytest.mark.unit


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    return CatalogGraph.load(catalog.write())


def test_all_valid_yields_no_findings(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    assert validate_schema(_graph(catalog), lambda data: []) == []


def test_invalid_object_becomes_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    graph = _graph(catalog)

    def check(data: dict) -> list[str]:
        return ["'title' is a required property (at )"] if data.get("type") == "Collection" else []

    findings = validate_schema(graph, check)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-SCH-001"
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert "'title' is a required property" in findings[0].message


def test_items_are_validated(catalog: CatalogBuilder) -> None:
    """Items never declare the schema URI but the schema pass still applies to them."""
    collection = catalog.collection("roads")
    collection.item("seg1")
    graph = _graph(catalog)

    def check(data: dict) -> list[str]:
        return ["bad item"] if data.get("type") == "Feature" else []

    findings = validate_schema(graph, check)
    assert len(findings) == 1
    assert findings[0].path == "roads/seg1/seg1.json"


def test_validator_failure_is_a_single_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("a")
    catalog.collection("b")
    graph = _graph(catalog)

    def boom(data: dict) -> list[str]:
        raise RuntimeError("could not reach schemas.portolan-sdi.org")

    findings = validate_schema(graph, boom)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-SCH-000"
    assert findings[0].severity is Severity.WARNING
    assert "could not reach" in findings[0].message


def test_missing_package_is_a_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rashid.schema as schema

    def raise_import(uri: str) -> None:
        raise ImportError("No module named 'jsonschema'")

    monkeypatch.setattr(schema, "default_validator", raise_import)
    findings = schema.validate_schema(_graph(catalog), None)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-SCH-000"
    assert "not installed" in findings[0].message


def test_schema_fetch_failure_is_a_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rashid.schema as schema

    def raise_fetch(uri: str) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(schema, "default_validator", raise_fetch)
    findings = schema.validate_schema(_graph(catalog), None)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-SCH-000"
    assert "could not run" in findings[0].message


def test_default_validator_used_when_none(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rashid.schema as schema

    monkeypatch.setattr(schema, "default_validator", lambda uri: lambda data: [])
    assert schema.validate_schema(_graph(catalog), None) == []


def test_unparseable_object_is_skipped(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "collection.json").write_text("{ broken", encoding="utf-8")
    graph = CatalogGraph.load(root)

    seen: list[str | None] = []

    def check(data: dict) -> list[str]:
        seen.append(data.get("type"))
        return []

    assert validate_schema(graph, check) == []
    assert "Collection" not in seen  # the broken collection never reached the validator


def test_schema_uri_prefers_root_declaration(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    assert _schema_uri_for(_graph(catalog)) == PORTOLAN_URI


def test_schema_uri_falls_back_to_default(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(root / "catalog.json", lambda d: d.__setitem__("stac_extensions", []))
    assert _schema_uri_for(CatalogGraph.load(root)) == DEFAULT_SCHEMA_URI


def test_default_validator_formats_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import rashid.schema as schema

    fake_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"links": {"type": "array"}},
        "required": ["id"],
    }
    monkeypatch.setattr(schema, "_fetch_schema", lambda uri: fake_schema)
    validate_object = default_validator("https://example.org/schema.json")

    assert validate_object({"id": "ok", "links": []}) == []
    messages = validate_object({"links": "not-an-array"})
    assert any("'id' is a required property (at )" == m for m in messages)
    assert any("/links" in m for m in messages)


def test_oneof_reports_the_matched_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A type-discriminated oneOf failure names the real cause, not the wrong branch."""
    import rashid.schema as schema

    fake_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "oneOf": [
            {"properties": {"type": {"const": "Catalog"}}, "required": ["type", "cat_only"]},
            {"properties": {"type": {"const": "Collection"}}, "required": ["type", "providers"]},
        ],
    }
    monkeypatch.setattr(schema, "_fetch_schema", lambda uri: fake_schema)
    validate_object = default_validator("https://example.org/schema.json")

    messages = validate_object({"type": "Collection"})
    assert len(messages) == 1
    # the Collection branch's real cause, not the misleading "'Catalog' was expected"
    assert "'providers' is a required property" in messages[0]
    assert "Catalog" not in messages[0]


def test_oneof_unknown_type_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A type matching no branch still yields a finding rather than crashing."""
    import rashid.schema as schema

    fake_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "oneOf": [
            {"properties": {"type": {"const": "Catalog"}}},
            {"properties": {"type": {"const": "Collection"}}},
        ],
    }
    monkeypatch.setattr(schema, "_fetch_schema", lambda uri: fake_schema)
    validate_object = default_validator("https://example.org/schema.json")

    messages = validate_object({"type": "Feature"})
    assert len(messages) == 1  # no branch matched; the whole context is reported


def test_long_messages_are_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    import rashid.schema as schema

    fake_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "properties": {"x": {"const": "A" * 400}},
        "required": ["x"],
    }
    monkeypatch.setattr(schema, "_fetch_schema", lambda uri: fake_schema)
    validate_object = default_validator("https://example.org/schema.json")

    (message,) = validate_object({"x": "B"})
    assert message.endswith("... (at /x)")


def test_non_https_schema_uri_is_rejected() -> None:
    """A file:// or other non-https schema URI must not be fetched (CWE-22 / SSRF)."""
    from rashid.schema import _fetch_schema

    with pytest.raises(ValueError, match="https"):
        _fetch_schema("file:///etc/passwd")


def test_runner_wires_schema_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def check(data: dict) -> list[str]:
        return ["bad root"] if data.get("type") == "Catalog" else []

    report = validate(root, schema=True, schema_validator=check)
    assert not report.passed
    assert any(f.rule_id == "PTL-SCH-001" for f in report.findings)


def test_schema_off_by_default(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    report = validate(root, schema_validator=lambda data: ["never seen"])
    assert all(f.rule_id != "PTL-SCH-001" for f in report.findings)


def test_disabling_sch_rule_skips_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    calls: list[dict] = []

    def check(data: dict) -> list[str]:
        calls.append(data)
        return ["bad"]

    report = validate(
        root,
        config=RulesConfig(disabled=frozenset({"PTL-SCH-001"})),
        schema=True,
        schema_validator=check,
    )
    assert all(f.rule_id != "PTL-SCH-001" for f in report.findings)
    assert calls == []  # the pass was skipped entirely, validator never invoked


@pytest.mark.network
def test_real_schema_accepts_the_builder(catalog: CatalogBuilder) -> None:
    import urllib.request

    try:
        urllib.request.urlopen(DEFAULT_SCHEMA_URI, timeout=5)  # noqa: S310
    except Exception:  # noqa: BLE001
        pytest.skip(f"{DEFAULT_SCHEMA_URI} unreachable")

    collection = catalog.collection("roads")
    collection.item("seg1")
    report = validate(catalog.write(), schema=True)
    schema_errors = [f for f in report.findings if f.rule_id == "PTL-SCH-001"]
    # Fetching the profile schema can fail after the reachability probe passed.
    skip_if_network_flaked(f.message for f in schema_errors)
    assert schema_errors == []  # the builder's tree satisfies the profile schema
