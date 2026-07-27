"""Tests for the PTL-ROL rules: item rollup presence and registration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reis import validate
from reis.model import Severity
from reis.rules.rollup import PARQUET_MEDIA_TYPE
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit

_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def _collection(root: Path) -> Path:
    return root / "scenes" / "collection.json"


def _item(root: Path, item_id: str) -> Path:
    return root / "scenes" / item_id / f"{item_id}.json"


def _scene_collection(catalog: CatalogBuilder, *item_ids: str) -> Path:
    """A raster collection whose scenes live on items, with no rollup."""
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


def _rollup_asset(**overrides: Any) -> dict[str, Any]:
    asset = {
        "href": "./items.parquet",
        "type": PARQUET_MEDIA_TYPE,
        "roles": ["stac-items"],
        "title": "STAC items as GeoParquet",
    }
    asset.update(overrides)
    return asset


def _rollup_link(**overrides: Any) -> dict[str, Any]:
    link = {
        "rel": "items",
        "href": "./items.parquet",
        "type": PARQUET_MEDIA_TYPE,
        "title": "STAC items as GeoParquet",
    }
    link.update(overrides)
    return link


def _register(root: Path, *, asset: dict[str, Any] | None = None, link: bool = True) -> None:
    def mutate(data: dict[str, Any]) -> None:
        if asset is not None:
            key = asset.pop("_key", "geoparquet-items")
            data["assets"][key] = asset
        if link:
            data["links"].append(_rollup_link())

    mutate_json(_collection(root), mutate)


# --- PTL-ROL-001: the rollup SHOULD exist -----------------------------------


def test_scene_collection_without_a_rollup_warns(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a", "scene-b")
    findings = findings_for(validate(root), "PTL-ROL-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert findings[0].path == "scenes/collection.json"
    assert "2 scene(s)" in findings[0].message


def test_scene_collection_with_a_rollup_is_clean(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a", "scene-b")
    _register(root, asset=_rollup_asset())
    report = validate(root)
    assert findings_for(report, "PTL-ROL-001") == []
    assert findings_for(report, "PTL-ROL-002") == []


def test_single_cog_collection_owes_no_rollup(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-072's shape: one COG at collection level, no items."""
    catalog.collection("scenes")
    root = catalog.write()
    mutate_json(
        _collection(root),
        lambda d: d["assets"]["data"].update({"href": "./scene.tif", "type": _COG_TYPE}),
    )
    assert findings_for(validate(root), "PTL-ROL-001") == []


def test_vector_collection_with_items_owes_no_rollup(catalog: CatalogBuilder) -> None:
    """The ratified SHOULD is raster-only; vector rollups are still incubating."""
    collection = catalog.collection("scenes")
    collection.item("seg1")
    collection.item("seg2")
    root = catalog.write()
    assert findings_for(validate(root), "PTL-ROL-001") == []


# --- PTL-ROL-002: both registrations, right role and type -------------------


def test_asset_without_a_link_is_flagged(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, asset=_rollup_asset(), link=False)
    findings = findings_for(validate(root), "PTL-ROL-002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "rel:'items' link" in findings[0].message


def test_link_without_an_asset_is_flagged(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, link=True)
    findings = findings_for(validate(root), "PTL-ROL-002")
    assert len(findings) == 1
    assert "no collection-level asset" in findings[0].message


def test_wrong_asset_key_is_flagged(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, asset=_rollup_asset(_key="items"))
    findings = findings_for(validate(root), "PTL-ROL-002")
    assert len(findings) == 1
    assert "keyed 'items'" in findings[0].message


def test_missing_role_is_flagged(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, asset=_rollup_asset(roles=["data"]))
    findings = findings_for(validate(root), "PTL-ROL-002")
    assert len(findings) == 1
    assert "'stac-items' role" in findings[0].message


def test_wrong_media_type_is_flagged_on_asset_and_link(catalog: CatalogBuilder) -> None:
    root = _scene_collection(catalog, "scene-a")
    _register(root, asset=_rollup_asset(type="application/octet-stream"))
    mutate_json(
        _collection(root),
        lambda d: d["links"][-1].__setitem__("type", "application/octet-stream"),
    )
    messages = [f.message for f in findings_for(validate(root), "PTL-ROL-002")]
    assert len(messages) == 2
    assert any("rollup asset" in m and "expected" in m for m in messages)
    assert any("rel:'items' link" in m and "expected" in m for m in messages)


def test_a_rollup_named_only_by_href_is_still_checked(catalog: CatalogBuilder) -> None:
    """A producer who gets key and role wrong still meant items.parquet."""
    root = _scene_collection(catalog, "scene-a")
    _register(root, asset=_rollup_asset(_key="index", roles=["data"]))
    messages = [f.message for f in findings_for(validate(root), "PTL-ROL-002")]
    assert any("keyed 'index'" in m for m in messages)
    assert any("'stac-items' role" in m for m in messages)


def test_collection_without_any_rollup_signal_is_out_of_scope(catalog: CatalogBuilder) -> None:
    catalog.collection("scenes").item("seg1")
    root = catalog.write()
    assert findings_for(validate(root), "PTL-ROL-002") == []
