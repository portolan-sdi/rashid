"""Exercise the shared shell through the validation API and real schema engines."""

from __future__ import annotations

from importlib import import_module

import pytest

from rashid import _store, validate
from rashid.api import ValidationOptions
from rashid.schema import validator_from_schema
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.integration

PROJECTION_URI = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"


def _assert_owner(name: str) -> None:
    # This is a move with unchanged behavior: reversion must fail ownership,
    # while the real-engine assertions below preserve each pass's policy.
    owner = import_module("rashid._jsonschema")
    assert hasattr(owner, "SchemaPass"), "the schema-pass shell must have one owner"
    assert isinstance(import_module(f"rashid.{name}")._PASS, owner.SchemaPass)


@pytest.mark.parametrize("name", ["structural", "schema", "extensions"])
def test_shared_shell_rebuilds_the_same_findings(name: str, catalog: CatalogBuilder) -> None:
    _assert_owner(name)
    collection = catalog.collection("a", stac_extensions=[PROJECTION_URI])
    collection.overrides["assets"] = {"data": {"href": "./data.parquet", "proj:code": 4326}}
    collection.overrides["extent"] = None
    collection.overrides["title"] = None
    catalog.collection("broken")
    root = catalog.write()
    (root / "broken" / "collection.json").write_text("{broken", encoding="utf-8")
    options = ValidationOptions(
        rules=[],
        data=False,
        structural=name == "structural",
        schema=name == "schema",
        extensions=name == "extensions",
    )

    first = validate(root, options=options)
    assert first.to_dict() == validate(root, options=options).to_dict()
    prefix = {"structural": "PTL-STR-", "schema": "PTL-SCH-", "extensions": "PTL-EXT-"}[name]
    findings = [f for f in first.findings if f.rule_id.startswith(prefix)]
    assert findings
    assert all(f.path == "a/collection.json" and f.object_id == "a" for f in findings)
    assert all(f.rule_id == f"{prefix}001" for f in findings)
    assert all(f.json_pointer is not None for f in findings)
    if name == "extensions":
        assert findings[0].message.startswith("Projection v2.0.0 schema validation failed:")
        assert findings[0].json_pointer == "/assets/data/proj:code"


@pytest.mark.parametrize("name", ["structural", "schema"])
def test_shared_shell_keeps_findings_before_a_systemic_failure(
    name: str, catalog: CatalogBuilder
) -> None:
    _assert_owner(name)
    catalog.collection("a")
    catalog.collection("b")
    # The real engine rejects a, then encounters an unresolvable ref on b.
    # No validator or schema-engine internals are mocked.
    validator = validator_from_schema(
        {
            "required": ["proof"],
            "if": {"properties": {"id": {"const": "b"}}},
            "then": {"$ref": "https://example.invalid/missing-schema.json"},
        }
    )
    options = ValidationOptions(
        rules=[],
        data=False,
        extensions=False,
        structural=name == "structural",
        schema=name == "schema",
        structural_validator=validator,
        schema_validator=validator,
    )

    report = validate(catalog.write(), options=options)
    assert len(report.findings) == 2
    (error,) = report.errors
    (warning,) = report.warnings
    assert error.path == "a/collection.json"
    assert error.json_pointer == ""
    assert "'proof' is a required property" in error.message
    assert warning.path == "."
    assert warning.message.startswith(f"{name} validation could not run:")
    assert "missing-schema.json" in warning.message


def test_profile_initialization_fetch_failure_is_a_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema_uri = "https://schemas.portolan-sdi.org/portolan/v99.0.0/schema.json"
    catalog.overrides["stac_extensions"] = [schema_uri]
    catalog.collection("a")
    catalog.collection("b")
    fetched: list[str] = []

    def unavailable(url: str) -> bytes:
        fetched.append(url)
        raise OSError("profile schema unavailable")

    monkeypatch.setattr(_store, "get", unavailable)
    report = validate(
        catalog.write(),
        options=ValidationOptions(
            rules=[],
            data=False,
            structural=False,
            extensions=False,
            schema=True,
            schema_allow_network=True,
        ),
    )

    assert fetched == [schema_uri]
    assert [finding.to_dict() for finding in report.findings] == [
        {
            "rule_id": "PTL-SCH-000",
            "severity": "warning",
            "message": "schema validation could not run: profile schema unavailable",
            "path": ".",
        }
    ]
    assert report.passed


def test_shared_extension_shell_recovers_per_uri(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    _assert_owner("extensions")
    good, dead = https_server.url("good.json"), https_server.url("dead.json")
    https_server.routes["/good.json"] = Route(body=b'{"required":["proof"]}')
    catalog.overrides["stac_extensions"] = [dead, good]
    for name in ("a", "b"):
        catalog.collection(name, stac_extensions=[dead, good])
    options = ValidationOptions(rules=[], data=False, structural=False, schema_allow_network=True)

    report = validate(catalog.write(), options=options)
    assert len(report.errors) == 3
    assert all(f.rule_id == "PTL-EXT-001" and good in f.message for f in report.errors)
    (warning,) = report.warnings
    assert warning.rule_id == "PTL-EXT-000"
    assert warning.actual == dead
    assert "3 object(s)" in warning.message
    assert [request.path for request in https_server.seen] == ["/dead.json", "/good.json"]
