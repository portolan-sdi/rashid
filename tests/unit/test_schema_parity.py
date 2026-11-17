"""The hand rules must cover the profile schema: ``schema_invalid => rashid error``.

This is issue #8's invariant, run offline against the bundled schema. The schema
pass stays opt-in precisely because the metadata rules restate its verdicts with
better messages and fix hints; this test is what makes that stance safe. If a
spec change lands in the schema without a matching hand rule, the mutation that
trips the schema here will produce a clean metadata report and fail the test.

Each case mutates one aspect of a conformant tree. The corpus is not exhaustive;
it covers the requirement families the schema encodes (titles, extents,
providers, license, assets, conformance).

Every case also runs against the draft profile schema on spec main, with the
tree declaring the draft's URI. That rehearses the next profile release: a
hand rule the draft needs fails here before the release is bundled.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rashid import validate
from rashid.catalog import CatalogGraph
from rashid.schema import DEFAULT_SCHEMA_URI, bundled_schema, validator_from_schema
from tests.conftest import CatalogBuilder, mutate_json

pytestmark = pytest.mark.unit

_DRAFT = Path(__file__).resolve().parents[1] / "fixtures" / "draft-profile-schema.json"


def _profiles() -> dict[str, tuple[str, dict[str, Any]]]:
    """Profile name -> (the URI the tree declares, the schema that judges it)."""
    bundled = bundled_schema(DEFAULT_SCHEMA_URI)
    assert bundled is not None
    draft = json.loads(_DRAFT.read_bytes())
    return {
        "bundled": (DEFAULT_SCHEMA_URI, bundled),
        "draft": (str(draft["$id"]).rstrip("#"), draft),
    }


_PROFILES = _profiles()


def _write(catalog: CatalogBuilder, uri: str) -> Path:
    catalog.overrides["stac_extensions"] = [uri]
    catalog.collection("roads", stac_extensions=[uri])
    return catalog.write()


Mutation = Callable[[Path], None]


def _drop_collection_title(root: Path) -> None:
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("title"))


def _break_bbox(root: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["extent"]["spatial"]["bbox"] = [[190.0, -95.0, 200.0, 95.0]]

    mutate_json(root / "roads" / "collection.json", mutate)


def _drop_providers(root: Path) -> None:
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("providers"))


def _bad_license(root: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["license"] = ""

    mutate_json(root / "roads" / "collection.json", mutate)


def _drop_asset_checksum(root: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        for asset in d.get("assets", {}).values():
            asset.pop("file:checksum", None)

    mutate_json(root / "roads" / "collection.json", mutate)


def _drop_conformance_uri(root: Path) -> None:
    def mutate(d: dict[str, Any]) -> None:
        d["stac_extensions"] = [
            uri for uri in d.get("stac_extensions", []) if "portolan" not in uri
        ]

    mutate_json(root / "catalog.json", mutate)


_MUTATIONS: dict[str, Mutation] = {
    "collection-without-title": _drop_collection_title,
    "out-of-range-bbox": _break_bbox,
    "collection-without-providers": _drop_providers,
    "empty-license": _bad_license,
    "asset-without-checksum": _drop_asset_checksum,
    "root-without-conformance-uri": _drop_conformance_uri,
}


def _schema_error_count(root: Path, schema: dict[str, Any]) -> int:
    check = validator_from_schema(schema)
    graph = CatalogGraph.load(root)
    return sum(
        len(check(node.data))
        for node in graph.iter("catalog", "collection", "item")
        if node.parse_error is None
    )


@pytest.mark.parametrize("profile", sorted(_PROFILES))
@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_schema_rejection_implies_metadata_error(
    catalog: CatalogBuilder, name: str, profile: str
) -> None:
    uri, schema = _PROFILES[profile]
    root = _write(catalog, uri)
    _MUTATIONS[name](root)

    if _schema_error_count(root, schema) == 0:
        pytest.skip(f"mutation {name!r} does not trip the {profile} schema")
    report = validate(root)
    assert report.errors, (
        f"the profile schema rejects {name!r} but the metadata pass reports no error; "
        "a hand rule is missing (see issue #8)"
    )


@pytest.mark.parametrize("profile", sorted(_PROFILES))
def test_conformant_tree_passes_both(catalog: CatalogBuilder, profile: str) -> None:
    """The corpus baseline: unmutated, both oracles agree the tree is clean."""
    uri, schema = _PROFILES[profile]
    root = _write(catalog, uri)
    assert _schema_error_count(root, schema) == 0
    assert validate(root).errors == []
