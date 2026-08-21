"""Extension pass tests.

Deterministic tests inject a fake ``Validator``; the real validator runs
offline against the vendored extension closure, so it is exercised directly
too — against both a field constraint (Projection's ``proj:code`` type) and a
structural one (Web Map Links' required map link), because an extension schema
constrains more than enum values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rashid import RulesConfig, validate
from rashid._jsonschema import SchemaError, vendored_extension_schemas
from rashid.catalog import CatalogGraph
from rashid.extensions import default_validator, validate_extensions
from rashid.model import Severity
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit

PROJECTION_URI = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
WEB_MAP_LINKS_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"
# Registered, but at a version the registry no longer pins.
RASTER_UNPINNED_URI = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"
# Never registered. The reproducer extension from issue #155.
EO_URI = "https://stac-extensions.github.io/eo/v2.0.0/schema.json"


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    return CatalogGraph.load(catalog.write())


def _ids(findings: list[Any]) -> list[str]:
    return [finding.rule_id for finding in findings]


# --- wiring, with an injected validator -----------------------------------


def test_all_valid_yields_no_findings(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    assert validate_extensions(_graph(catalog), lambda data, uri: []) == []


def test_violation_becomes_an_error_carrying_the_pointer(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", stac_extensions=[PROJECTION_URI])

    def check(data: dict, uri: str) -> list[SchemaError]:
        if data.get("type") == "Collection":
            return [SchemaError(message="4326 is not of type 'string'", json_pointer="/x")]
        return []

    findings = validate_extensions(_graph(catalog), check)
    assert len(findings) == 1
    assert findings[0].rule_id == "PTL-EXT-001"
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert findings[0].json_pointer == "/x"
    assert "Projection v2.0.0" in findings[0].message
    assert "4326 is not of type 'string'" in findings[0].message


def test_the_portolan_profile_uri_is_not_validated_here(catalog: CatalogBuilder) -> None:
    """The --schema pass and PTL-CNF-001 own it; validating twice doubles findings."""
    catalog.collection("roads")
    calls: list[str] = []

    def check(data: dict, uri: str) -> list[SchemaError]:
        calls.append(uri)
        return []

    validate_extensions(_graph(catalog), check)
    assert calls == []


def test_a_systemic_validator_failure_reports_once(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    catalog.collection("rails", stac_extensions=[PROJECTION_URI])

    def explode(data: dict, uri: str) -> list[SchemaError]:
        raise RuntimeError("registry is broken")

    findings = validate_extensions(_graph(catalog), explode)
    assert _ids(findings) == ["PTL-EXT-000"]
    assert findings[0].severity is Severity.WARNING
    assert "registry is broken" in findings[0].message


# --- unregistered extensions ----------------------------------------------


def test_an_unregistered_extension_is_reported_not_validated(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", stac_extensions=[EO_URI])
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-002"]
    assert findings[0].severity is Severity.INFO
    assert findings[0].actual == EO_URI


def test_an_unregistered_extension_is_reported_once_for_the_catalog(
    catalog: CatalogBuilder,
) -> None:
    """One notice per URI, not per object: a catalog declares it everywhere."""
    for name in ("roads", "rails", "rivers"):
        catalog.collection(name, stac_extensions=[EO_URI])
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-002"]
    assert "3 object(s)" in findings[0].message


def test_a_registered_extension_at_an_unpinned_version_is_not_validated(
    catalog: CatalogBuilder,
) -> None:
    """Never judge v1.1.0 content by the v2.0.0 schema; PTL-CNF-004 owns the version."""
    catalog.collection("roads", stac_extensions=[RASTER_UNPINNED_URI])
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-002"]
    assert findings[0].actual == RASTER_UNPINNED_URI


def test_an_unregistered_extension_is_fetched_when_network_is_allowed(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog.collection("roads", stac_extensions=[EO_URI])
    eo_schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["eo:proof"],
    }
    from rashid import extensions as extensions_module

    monkeypatch.setattr(extensions_module, "_fetch_schema", lambda uri: eo_schema)
    findings = validate_extensions(_graph(catalog), allow_network=True)
    assert _ids(findings) == ["PTL-EXT-001"]
    assert "'eo:proof' is a required property" in findings[0].message


# --- the real vendored validator ------------------------------------------


def test_the_vendored_closure_covers_every_registered_extension() -> None:
    """Each pinned URI resolves offline, so no pinned extension silently skips.

    This is the guard that keeps the profile's registry and the vendored tree
    in step. Adding an extension to the registry without re-running
    ``scripts/vendor_stac_schemas.py`` fails here rather than degrading every
    catalog that declares it to a PTL-EXT-002 notice.
    """
    from importlib import resources

    from rashid.rules.conformance import registered_extension

    store = vendored_extension_schemas()
    registry = json.loads(
        resources.files("rashid")
        .joinpath("_schemas/extension-registry.json")
        .read_text(encoding="utf-8")
    )
    pinned = [
        uri
        for uri in registry["extensions"].values()
        # The profile schema is vendored under _schemas/portolan/ and applied
        # by the --schema pass, so it is deliberately absent from this store.
        if registered_extension(uri) is not None and "/portolan/v" not in uri
    ]
    assert pinned, "the vendored registry lists no extensions"
    assert [uri for uri in pinned if uri not in store] == []


def test_projection_type_violation_is_caught_offline(catalog: CatalogBuilder) -> None:
    """Issue #154's reproducer: proj:code must be a string or null."""
    collection = catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    collection.overrides["assets"] = {
        "data": {
            "href": "./roads.parquet",
            "type": "application/vnd.apache.parquet",
            "roles": ["data"],
            "proj:code": 4326,
        }
    }
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-001"]
    assert findings[0].json_pointer == "/assets/data/proj:code"
    assert findings[0].message.endswith("4326 is not of type 'string', 'null'")


def test_a_valid_projection_value_passes_offline(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    collection.overrides["assets"] = {
        "data": {
            "href": "./roads.parquet",
            "type": "application/vnd.apache.parquet",
            "roles": ["data"],
            "proj:code": "EPSG:4326",
        }
    }
    assert validate_extensions(_graph(catalog)) == []


def test_a_missing_required_map_link_is_caught_offline(catalog: CatalogBuilder) -> None:
    """A structural constraint, not a field enum. The real moldova-geodata failure.

    The collection declares Web Map Links and carries a ``service`` link, which
    is not one of the relations the extension's ``contains`` constraint accepts.
    """
    catalog.collection("roads", stac_extensions=[WEB_MAP_LINKS_URI])
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-001"]
    assert findings[0].json_pointer == "/links"
    # The message names the requirement, never the whole links array.
    assert "'wms'" in findings[0].message
    assert "href" not in findings[0].message


def test_a_present_map_link_passes_offline(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", stac_extensions=[WEB_MAP_LINKS_URI])
    built = collection.build()
    collection.overrides["links"] = [
        *built["links"],
        # wms:layers is required on a wms link; the extension constrains the
        # link's own shape, not only that some map link exists.
        {
            "rel": "wms",
            "href": "https://example.org/wms",
            "type": "image/png",
            "wms:layers": ["roads"],
        },
    ]
    assert validate_extensions(_graph(catalog)) == []


def test_the_default_validator_rejects_a_non_https_fetch() -> None:
    check = default_validator(allow_network=True)
    with pytest.raises(ValueError, match="https"):
        check({}, "file:///etc/passwd")


# --- runner and config wiring ---------------------------------------------


def test_the_pass_runs_by_default(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    collection.overrides["assets"] = {
        "data": {
            "href": "./roads.parquet",
            "type": "application/vnd.apache.parquet",
            "roles": ["data"],
            "proj:code": 4326,
        }
    }
    report = validate(catalog.write(), data=False)
    assert "PTL-EXT-001" in _ids(report.findings)


def test_extensions_false_skips_the_pass(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    collection.overrides["assets"] = {
        "data": {
            "href": "./roads.parquet",
            "type": "application/vnd.apache.parquet",
            "roles": ["data"],
            "proj:code": 4326,
        }
    }
    report = validate(catalog.write(), data=False, extensions=False)
    assert "PTL-EXT-001" not in _ids(report.findings)


def test_disabling_the_rule_skips_the_pass(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    collection.overrides["assets"] = {
        "data": {
            "href": "./roads.parquet",
            "type": "application/vnd.apache.parquet",
            "roles": ["data"],
            "proj:code": 4326,
        }
    }
    report = validate(
        catalog.write(), config=RulesConfig(disabled=frozenset({"PTL-EXT-001"})), data=False
    )
    assert "PTL-EXT-001" not in _ids(report.findings)


def test_a_missing_jsonschema_degrades_to_one_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    from rashid import extensions as extensions_module

    def no_jsonschema(*_args: object, **_kwargs: object) -> None:
        raise ImportError("No module named 'jsonschema'")

    monkeypatch.setattr(extensions_module, "default_validator", no_jsonschema)
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-000"]
    assert findings[0].severity is Severity.WARNING
    assert "jsonschema" in findings[0].message


def test_an_unparseable_object_is_skipped(catalog: CatalogBuilder) -> None:
    """The runner reports it as PTL-GEN-001; this pass has no JSON to validate."""
    catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    root = catalog.write()
    (root / "roads" / "collection.json").write_text("{ not json", encoding="utf-8")
    assert validate_extensions(CatalogGraph.load(root)) == []


def test_a_pinned_extension_missing_from_the_wheel_is_reported_not_validated(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vendoring gap must degrade to PTL-EXT-002, never to a false pass.

    test_the_vendored_closure_covers_every_registered_extension stops this
    reaching a release. If one ever did, the object is unvalidated, and the
    report has to say so.
    """
    catalog.collection("roads", stac_extensions=[PROJECTION_URI])
    from rashid import extensions as extensions_module

    monkeypatch.setattr(extensions_module, "vendored_extension_schemas", dict)
    findings = validate_extensions(_graph(catalog))
    assert _ids(findings) == ["PTL-EXT-002"]
    assert findings[0].actual == PROJECTION_URI


@pytest.mark.parametrize(
    ("subschema", "expected"),
    [
        ({"properties": {"rel": {"const": "wms"}}}, "rel of 'wms'"),
        ({"properties": {"rel": {"enum": ["a", "b"]}}}, "rel of 'a' or 'b'"),
        ({"const": "x"}, "'x'"),
        ({"enum": ["x", "y"]}, "'x' or 'y'"),
    ],
)
def test_a_contains_failure_names_the_requirement(subschema: dict, expected: str) -> None:
    """The raw jsonschema message prints the array and never the requirement."""
    from rashid._jsonschema import _contains_requirement

    assert _contains_requirement(subschema) == expected


@pytest.mark.parametrize(
    "subschema",
    [
        True,
        {"properties": {"rel": {"type": "string"}}},
        {"type": "object"},
    ],
)
def test_an_inexpressible_contains_requirement_falls_back(subschema: object) -> None:
    """No const or enum to name, so describe() keeps the raw message."""
    from rashid._jsonschema import _contains_requirement

    assert _contains_requirement(subschema) is None


def test_the_extension_store_is_empty_without_a_vendored_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build with no extensions/ tree yields no store, rather than raising."""
    from importlib import resources

    from rashid import _jsonschema as jsonschema_mod

    package_root = tmp_path / "rashid"
    (package_root / "_schemas").mkdir(parents=True)
    original = resources.files
    monkeypatch.setattr(
        resources,
        "files",
        lambda package: package_root if package == "rashid" else original(package),
    )
    jsonschema_mod.vendored_extension_schemas.cache_clear()
    try:
        assert jsonschema_mod.vendored_extension_schemas() == {}
    finally:
        jsonschema_mod.vendored_extension_schemas.cache_clear()
