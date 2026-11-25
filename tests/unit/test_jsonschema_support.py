"""The shared jsonschema layer: vendored registry and error narrowing."""

from __future__ import annotations

import pytest

from rashid_core._jsonschema import _GEOJSON_BASE, STAC_BASE, SchemaError, describe, stac_registry

pytestmark = pytest.mark.unit


def test_registry_serves_the_stac_roots() -> None:
    registry = stac_registry()
    for path in (
        "catalog-spec/json-schema/catalog.json",
        "collection-spec/json-schema/collection.json",
        "item-spec/json-schema/item.json",
    ):
        assert registry.contents(f"{STAC_BASE}{path}")["$id"] == f"{STAC_BASE}{path}"


def test_registry_serves_the_geojson_schemas() -> None:
    """item.json's geometry model refs geojson.org; those must resolve offline."""
    registry = stac_registry()
    assert registry.contents(f"{_GEOJSON_BASE}Feature.json")
    assert registry.contents(f"{_GEOJSON_BASE}Geometry.json")


def test_registry_normalizes_malformed_ids() -> None:
    """Upstream common.json declares a malformed $id; the registry must key it
    (and rebase its relative $refs) on the canonical retrieval URL instead."""
    url = f"{STAC_BASE}item-spec/json-schema/common.json"
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


@pytest.mark.parametrize(
    ("schema", "instance", "message", "pointer"),
    [
        (
            {"properties": {"collection": {"not": {}}}},
            {"collection": "roads"},
            "'collection' is not allowed here",
            "/collection",
        ),
        (
            {"properties": {"nested": {"properties": {"a/b~c": {"not": {}}}}}},
            {"nested": {"a/b~c": "value"}},
            "'a/b~c' is not allowed here",
            "/nested/a~1b~0c",
        ),
        ({"items": {"not": {}}}, ["value"], "'0' is not allowed here", "/0"),
        ({"properties": {"": {"not": {}}}}, {"": 1}, "'' is not allowed here", "/"),
        ({"not": {}}, {}, "document is not allowed here", ""),
    ],
)
def test_describe_names_a_member_that_must_be_absent(
    schema: dict, instance: object, message: str, pointer: str
) -> None:
    from jsonschema import Draft7Validator

    (raw,) = Draft7Validator(schema).iter_errors(instance)
    assert describe(raw) == SchemaError(message=message, json_pointer=pointer)


def test_describe_preserves_other_not_errors() -> None:
    from jsonschema import Draft7Validator

    (raw,) = Draft7Validator({"not": {"type": "string"}}).iter_errors("value")
    assert describe(raw) == SchemaError(message=raw.message, json_pointer="")
