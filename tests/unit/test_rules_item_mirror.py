"""Tests for the PTL-MIR rules: item mirror presence and registration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rashid import validate
from rashid.model import Severity
from rashid.rules.item_mirror import MIRROR_ROLE, PARQUET_MEDIA_TYPE
from tests.conftest import (
    CatalogBuilder,
    findings_for,
    mutate_json,
    nest_items_under_organizing_catalog,
)

pytestmark = pytest.mark.unit

_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def _collection(root: Path) -> Path:
    return root / "scenes" / "collection.json"


def _item(root: Path, item_id: str) -> Path:
    return root / "scenes" / item_id / f"{item_id}.json"


def _scene_collection(catalog: CatalogBuilder, *item_ids: str) -> Path:
    """A raster collection whose scenes live on items, with no mirror."""
    collection = catalog.collection("scenes")
    for item_id in item_ids:
        collection.item(item_id)
    root = catalog.write()
    mutate_json(_collection(root), lambda d: d["assets"].pop("data", None))
    for item_id in item_ids:
        mutate_json(
            _item(root, item_id),
            lambda d, i=item_id: d["assets"]["data"].update(
                {"href": f"./{i}.tif", "type": _COG_TYPE}
            ),
        )
    return root


def _mirror_asset(**overrides: Any) -> dict[str, Any]:
    asset = {
        "href": "./items.parquet",
        "type": PARQUET_MEDIA_TYPE,
        "roles": [MIRROR_ROLE],
        "title": "STAC items as GeoParquet",
    }
    asset.update(overrides)
    return asset


def _register(root: Path, asset: dict[str, Any]) -> None:
    def mutate(data: dict[str, Any]) -> None:
        key = asset.pop("_key", "items-parquet")
        data["assets"][key] = asset

    mutate_json(_collection(root), mutate)


# --- PTL-MIR-001: the mirror SHOULD exist -----------------------------------


def test_scene_collection_without_a_mirror_warns(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a", "scene-b")
    findings = findings_for(validate(root), "PTL-MIR-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert findings[0].path == "scenes/collection.json"
    assert "2 scene(s)" in findings[0].message


def test_wrong_cog_type_does_not_hide_the_mirror_requirement(
    catalog: CatalogBuilder,
) -> None:
    root = _scene_collection(catalog, "scene-a", "scene-b")
    for item_id in ("scene-a", "scene-b"):
        mutate_json(
            _item(root, item_id),
            lambda d: d["assets"]["data"].__setitem__("type", "image/tiff; application=geotiff"),
        )
    report = validate(root)
    mirror_findings = findings_for(report, "PTL-MIR-001")
    assert len(mirror_findings) == 1
    assert "2 scene(s)" in mirror_findings[0].message
    assert len(findings_for(report, "PTL-AST-006")) == 2


def test_source_geotiff_items_do_not_create_a_mirror_requirement(
    catalog: CatalogBuilder,
) -> None:
    root = _scene_collection(catalog, "source-a", "source-b")
    for item_id in ("source-a", "source-b"):
        mutate_json(
            _item(root, item_id),
            lambda d: d["assets"]["data"].update(
                {
                    "type": "image/tiff; application=geotiff",
                    "roles": ["data", "source"],
                }
            ),
        )
    report = validate(root)
    assert findings_for(report, "PTL-MIR-001") == []
    assert findings_for(report, "PTL-AST-006") == []


def test_scene_collection_organized_by_a_catalog_still_owes_a_mirror(
    catalog: CatalogBuilder,
) -> None:
    """core.md permits a catalog below a collection to group its items.

    The scenes are then not the collection's containment children, but they
    are still its items, so the mirror is still owed. Resolving ownership by
    direct containment alone makes this rule skip without saying so.
    """
    root = _scene_collection(catalog, "scene-a", "scene-b")
    nest_items_under_organizing_catalog(root, root / "scenes")
    findings = findings_for(validate(root), "PTL-MIR-001")
    assert len(findings) == 1
    assert "2 scene(s)" in findings[0].message


def test_scene_collection_with_a_mirror_is_clean(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a", "scene-b")
    _register(root, _mirror_asset())
    report = validate(root)
    assert findings_for(report, "PTL-MIR-001") == []
    assert findings_for(report, "PTL-MIR-002") == []


def test_a_mirror_needs_no_items_link(catalog: CatalogBuilder) -> None:
    """The stac-geoparquet spec asks for the asset alone (PORTO-FMT-041)."""
    root = _scene_collection(catalog, "scene-a")
    _register(root, _mirror_asset())
    assert [link for link in _links(root) if link.get("rel") == "items"] == []
    assert findings_for(validate(root), "PTL-MIR-002") == []


def test_single_cog_collection_owes_no_mirror(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-072's shape: one COG at collection level, no items."""
    catalog.collection("scenes")
    root = catalog.write()
    mutate_json(
        _collection(root),
        lambda d: d["assets"]["data"].update({"href": "./scene.tif", "type": _COG_TYPE}),
    )
    assert findings_for(validate(root), "PTL-MIR-001") == []


def test_vector_collection_with_items_owes_no_mirror(catalog: CatalogBuilder) -> None:
    """The ratified SHOULD is raster-only; mirroring vector is still incubating."""
    collection = catalog.collection("scenes")
    collection.item("seg1")
    collection.item("seg2")
    root = catalog.write()
    assert findings_for(validate(root), "PTL-MIR-001") == []


# --- PTL-MIR-002: the right role and media type -----------------------------


def test_missing_role_is_flagged(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, _mirror_asset(roles=["data"]))
    findings = findings_for(validate(root), "PTL-MIR-002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert f"'{MIRROR_ROLE}' role" in findings[0].message


def test_wrong_media_type_is_flagged(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, _mirror_asset(type="application/octet-stream"))
    findings = findings_for(validate(root), "PTL-MIR-002")
    assert len(findings) == 1
    assert "mirror asset" in findings[0].message
    assert "expected" in findings[0].message


def test_any_asset_key_is_accepted(catalog: CatalogBuilder) -> None:
    """The stac-geoparquet spec names no key, so the producer picks one."""
    root = _scene_collection(catalog, "scene-a")
    _register(root, _mirror_asset(_key="geoparquet-items"))
    assert findings_for(validate(root), "PTL-MIR-002") == []


def test_a_mirror_named_only_by_href_is_still_checked(catalog: CatalogBuilder) -> None:
    """A producer who gets the role wrong still meant items.parquet."""
    root = _scene_collection(catalog, "scene-a")
    _register(root, _mirror_asset(_key="index", roles=["data"]))
    messages = [f.message for f in findings_for(validate(root), "PTL-MIR-002")]
    assert any(f"'{MIRROR_ROLE}' role" in m for m in messages)


def test_mirror_on_an_itemless_collection_is_left_to_the_structural_rule(
    catalog: CatalogBuilder,
) -> None:
    """A mirror over nothing is a missing item tree, which PTL-COL-005 reports.

    Neither PTL-MIR rule piles on: PTL-MIR-001 nudges towards a mirror that is
    already there, and PTL-MIR-002 finds the registration correct.
    """
    catalog.collection("scenes")
    root = catalog.write()
    _register(root, _mirror_asset())
    report = validate(root)
    assert findings_for(report, "PTL-MIR-001") == []
    assert findings_for(report, "PTL-MIR-002") == []
    assert len(findings_for(report, "PTL-COL-005")) == 1


def test_collection_without_any_mirror_signal_is_out_of_scope(catalog: CatalogBuilder) -> None:
    catalog.collection("scenes").item("seg1")
    root = catalog.write()
    assert findings_for(validate(root), "PTL-MIR-002") == []


def _links(root: Path) -> list[dict[str, Any]]:
    data = json.loads(_collection(root).read_text())
    links = data.get("links")
    return links if isinstance(links, list) else []
