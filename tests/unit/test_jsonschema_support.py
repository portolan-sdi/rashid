"""The shared jsonschema layer: vendored registry and error narrowing."""

from __future__ import annotations

import pytest

from rashid._jsonschema import _GEOJSON_BASE, _STAC_BASE, SchemaError, describe, stac_registry

pytestmark = pytest.mark.unit


def test_registry_serves_the_stac_roots() -> None:
    registry = stac_registry()
    for path in (
        "catalog-spec/json-schema/catalog.json",
        "collection-spec/json-schema/collection.json",
        "item-spec/json-schema/item.json",
    ):
        assert registry.contents(f"{_STAC_BASE}{path}")["$id"] == f"{_STAC_BASE}{path}"


def test_registry_serves_the_geojson_schemas() -> None:
    """item.json's geometry model refs geojson.org; those must resolve offline."""
    registry = stac_registry()
    assert registry.contents(f"{_GEOJSON_BASE}Feature.json")
    assert registry.contents(f"{_GEOJSON_BASE}Geometry.json")


def test_registry_normalizes_malformed_ids() -> None:
    """Upstream common.json declares a malformed $id; the registry must key it
    (and rebase its relative $refs) on the canonical retrieval URL instead."""
    url = f"{_STAC_BASE}item-spec/json-schema/common.json"
    assert stac_registry().contents(url)["$id"] == url


def test_schema_error_str_carries_the_pointer() -> None:
    assert str(SchemaError(message="bad", json_pointer="/links/0")) == "bad (at /links/0)"


def test_describe_truncates_long_messages() -> None:
    from jsonschema import Draft7Validator

    validator = Draft7Validator({"properties": {"x": {"const": "A" * 400}}})
    (raw,) = validator.iter_errors({"x": "B"})
    error = describe(raw)
    assert len(error.message) == 300
    assert error.message.endswith("...")
    assert error.json_pointer == "/x"
