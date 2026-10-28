"""jsonschema-rs and jsonschema must reach the same verdict on every object.

``rashid._jsonschema.screened`` trusts ``jsonschema-rs`` to pass a valid
object and only asks ``jsonschema`` about a rejected one. Where the engines
disagree, a finding disappears (Rust passes what Python rejects) or a valid
object costs a Python call (the reverse). These tests compile every schema
rashid bundles with both engines and compare the verdicts over the fixture
objects, valid and mutated.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator
from jsonschema.validators import validator_for

from rashid._jsonschema import (
    _STAC_BASE,
    compile_rust,
    extension_registry,
    extension_rust_registry,
    screened,
    specifications_rust_registry,
    stac_registry,
    stac_rust_registry,
    vendored_extension_schemas,
)
from rashid.schema import _bundled_schemas
from rashid.structural import _ROOT_SCHEMAS

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(obj: dict[str, Any]) -> None:
        obj[key] = value

    return mutate


def _drop(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(obj: dict[str, Any]) -> None:
        obj.pop(key, None)

    return mutate


def _break_assets(obj: dict[str, Any]) -> None:
    for asset in obj.get("assets", {}).values():
        asset["href"] = 7
        asset["file:size"] = "big"
        asset["file:checksum"] = "not-hex"
        asset["proj:code"] = 4326


def _break_links(obj: dict[str, Any]) -> None:
    obj["links"] = [{"rel": "self"}, {"href": "./x.json"}, "string"]


def _break_extent(obj: dict[str, Any]) -> None:
    obj["extent"] = {"spatial": {"bbox": [[200, 95]]}, "temporal": {"interval": [["x"]]}}


def _break_properties(obj: dict[str, Any]) -> None:
    obj["properties"] = {"datetime": "yesterday", "proj:code": 4326, "table:columns": "x"}


_MUTATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "drop-id": _drop("id"),
    "drop-links": _drop("links"),
    "drop-stac-version": _drop("stac_version"),
    "bad-stac-version": _set("stac_version", 110),
    "bad-type": _set("type", "Dataset"),
    "bad-extensions": _set("stac_extensions", ["not a uri", 3]),
    "bad-bbox": _set("bbox", ["a", "b"]),
    "bad-geometry": _set("geometry", {"type": "Point", "coordinates": "here"}),
    "bad-license": _set("license", ""),
    "bad-title": _set("title", 7),
    "bad-providers": _set("providers", [{"roles": "host"}]),
    "bad-assets": _break_assets,
    "bad-links": _break_links,
    "bad-extent": _break_extent,
    "bad-properties": _break_properties,
}


def _fixture_objects() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_FIXTURES.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("type") not in (
            "Catalog",
            "Collection",
            "Feature",
        ):
            continue
        name = str(path.relative_to(_FIXTURES))
        out.append((name, data))
        for label, mutate in _MUTATIONS.items():
            mutated = copy.deepcopy(data)
            mutate(mutated)
            out.append((f"{name}[{label}]", mutated))
    return out


_OBJECTS = _fixture_objects()


def _pairs() -> list[tuple[str, Any, Any]]:
    """(label, jsonschema validator, jsonschema-rs validator) for every schema rashid runs."""
    pairs: list[tuple[str, Any, Any]] = []
    for stac_type, path in _ROOT_SCHEMAS.items():
        root = {"$ref": f"{_STAC_BASE}{path}"}
        pairs.append(
            (
                f"stac:{stac_type}",
                Draft7Validator(root, registry=stac_registry()),
                compile_rust(root, stac_rust_registry(), draft7=True),
            )
        )
    for uri, schema in sorted(_bundled_schemas().items()):
        pairs.append(
            (
                f"profile:{uri}",
                Draft7Validator(schema),
                compile_rust(schema, specifications_rust_registry(), draft7=True),
            )
        )
    for uri, schema in sorted(vendored_extension_schemas().items()):
        pairs.append(
            (
                f"extension:{uri}",
                validator_for(schema)(schema, registry=extension_registry()),
                compile_rust(schema, extension_rust_registry()),
            )
        )
    return pairs


_PAIRS = _pairs()


def test_the_corpus_holds_valid_and_invalid_objects() -> None:
    """Parity over a corpus of only valid objects would prove nothing."""
    python = next(p for label, p, _ in _PAIRS if label == "stac:Feature")
    items = [data for _, data in _OBJECTS if data.get("type") == "Feature"]
    verdicts = {python.is_valid(data) for data in items}
    assert verdicts == {True, False}


@pytest.mark.parametrize(("label", "python", "rust"), _PAIRS, ids=[p[0] for p in _PAIRS])
def test_every_bundled_schema_compiles_offline_in_rust(label: str, python: Any, rust: Any) -> None:
    """A schema Rust cannot compile falls back to Python alone: correct, but slow."""
    assert rust is not None, f"jsonschema-rs could not compile {label}"


@pytest.mark.parametrize(("label", "python", "rust"), _PAIRS, ids=[p[0] for p in _PAIRS])
def test_rust_and_python_agree_on_every_fixture_object(label: str, python: Any, rust: Any) -> None:
    disagreements = [
        name for name, data in _OBJECTS if rust.is_valid(data) != python.is_valid(data)
    ]
    assert disagreements == []


def test_screened_skips_python_for_a_valid_object() -> None:
    class Rejects:
        def iter_errors(self, data: Any) -> Any:
            raise AssertionError("jsonschema ran for an object Rust passed")

    class Passes:
        def is_valid(self, data: Any) -> bool:
            return True

    assert screened(Rejects(), Passes())({"x": 1}) == []


def test_screened_asks_python_when_rust_rejects_or_is_missing() -> None:
    python = Draft7Validator({"properties": {"x": {"type": "string"}}})
    rust = compile_rust({"properties": {"x": {"type": "string"}}}, None, draft7=True)
    for validate in (screened(python, rust), screened(python, None)):
        (error,) = validate({"x": 1})
        assert error.json_pointer == "/x"
        assert validate({"x": "a"}) == []


def test_compile_rust_returns_none_for_an_unresolvable_ref() -> None:
    schema = {"$ref": "https://example.invalid/missing.json"}
    assert compile_rust(schema, specifications_rust_registry()) is None
