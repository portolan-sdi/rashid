"""Data pass over an item rollup: ids must match the items, and only that.

The rollup written here is deliberately the sort of GeoParquet file the data
rules reject for a data asset — geometries interleaved across the extent, no
covering column, one row per row group. formats.md exempts rollups from those
rules (PORTO-FMT-043), so the only finding a divergent rollup earns is
``PTL-DAT-016``.

Needs the ``reis[data]`` extra; skips without it. Fully local — no network.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")

from reis.catalog import CatalogGraph  # noqa: E402
from reis.data import (  # noqa: E402
    DAT_ORDERING,
    DAT_ROLLUP,
    DAT_ROWGROUP_STATS,
    DAT_TABULAR,
    validate_data,
)
from reis.model import Severity  # noqa: E402
from tests.conftest import CatalogBuilder, mutate_json  # noqa: E402
from tests.integration import _data_assets as assets  # noqa: E402

pytestmark = pytest.mark.integration

_PARQUET_TYPE = "application/vnd.apache.parquet"
_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_ITEM_IDS = ["scene-a", "scene-b"]


def _cog_asset(href: str) -> dict[str, Any]:
    return {"href": href, "type": _COG_TYPE, "roles": ["data"]}


def _rollup_asset() -> dict[str, Any]:
    return {
        "href": "./items.parquet",
        "type": _PARQUET_TYPE,
        "roles": ["stac-items"],
        "title": "STAC items as GeoParquet",
    }


def _patch_checksum(node_json: Path, asset_path: Path, key: str = "data") -> None:
    payload = asset_path.read_bytes()
    mutate_json(
        node_json,
        lambda d: d["assets"][key].update(
            {"file:size": len(payload), "file:checksum": assets.multihash(payload)}
        ),
    )


def _build(root: Path, rollup_ids: list[str] | None) -> Path:
    """A raster scene collection with a rollup over ``rollup_ids``."""
    cat = CatalogBuilder(root)
    col = cat.collection("scenes", assets={"geoparquet-items": _rollup_asset()})
    for item_id in _ITEM_IDS:
        col.item(item_id, assets={"data": _cog_asset(f"./{item_id}.tif")})
    cat.write()

    scenes = root / "scenes"
    mutate_json(
        scenes / "collection.json",
        lambda d: d["links"].append(
            {"rel": "items", "href": "./items.parquet", "type": _PARQUET_TYPE}
        ),
    )
    assets.write_item_rollup(scenes / "items.parquet", rollup_ids)
    for item_id in _ITEM_IDS:
        assets.write_cog(scenes / item_id / f"{item_id}.tif", size=256)
        _patch_checksum(scenes / item_id / f"{item_id}.json", scenes / item_id / f"{item_id}.tif")
    _patch_checksum(scenes / "collection.json", scenes / "items.parquet", "geoparquet-items")
    return root


@pytest.fixture(scope="module")
def matching(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("rollup") / "catalog", list(_ITEM_IDS))


@pytest.fixture
def catalog_root(matching: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "catalog"
    shutil.copytree(matching, dst)
    return dst


def _rollup_findings(root: Path) -> list:
    return [f for f in validate_data(CatalogGraph.load(root)) if f.rule_id == DAT_ROLLUP]


def _rewrite_rollup(root: Path, ids: list[str] | None) -> None:
    path = root / "scenes" / "items.parquet"
    assets.write_item_rollup(path, ids)
    _patch_checksum(root / "scenes" / "collection.json", path, "geoparquet-items")


def test_matching_rollup_is_clean(catalog_root: Path) -> None:
    findings = validate_data(CatalogGraph.load(catalog_root))
    assert findings == [], [f"{f.rule_id} {f.message}" for f in findings]


def test_rollup_is_exempt_from_the_geoparquet_data_rules(catalog_root: Path) -> None:
    """The rollup violates ordering and statistics; neither applies to it."""
    ids = {f.rule_id for f in validate_data(CatalogGraph.load(catalog_root))}
    assert DAT_ORDERING not in ids
    assert DAT_ROWGROUP_STATS not in ids
    assert DAT_TABULAR not in ids


def test_rollup_missing_an_item_is_flagged(catalog_root: Path) -> None:
    _rewrite_rollup(catalog_root, ["scene-a"])
    findings = _rollup_findings(catalog_root)
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "scenes/collection.json"
    assert "1 item(s) absent" in findings[0].message
    assert "scene-b" in findings[0].message


def test_rollup_row_without_an_item_is_flagged(catalog_root: Path) -> None:
    _rewrite_rollup(catalog_root, [*_ITEM_IDS, "scene-c"])
    findings = _rollup_findings(catalog_root)
    assert len(findings) == 1
    assert "1 rollup row(s) with no item" in findings[0].message
    assert "scene-c" in findings[0].message


def test_rollup_without_an_id_column_is_flagged(catalog_root: Path) -> None:
    _rewrite_rollup(catalog_root, None)
    findings = _rollup_findings(catalog_root)
    assert len(findings) == 1
    assert "no 'id' column" in findings[0].message


def test_itemless_collection_rollup_is_not_compared(tmp_path: Path) -> None:
    """Nothing to compare against, so the check stays silent."""
    root = _build(tmp_path / "catalog", list(_ITEM_IDS))
    for item_id in _ITEM_IDS:
        shutil.rmtree(root / "scenes" / item_id)
    mutate_json(
        root / "scenes" / "collection.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "item"]),
    )
    assert _rollup_findings(root) == []
