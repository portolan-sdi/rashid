"""Data pass over an item mirror: ids must match the items, storage rules bind.

A mirror is a GeoParquet file like any other, so the storage rules apply to it
(PORTO-FMT-043) on top of the agreement check that is its own (PORTO-FMT-042).
These tests cover both halves: a conformant mirror whose ids match is clean, a
divergent one earns ``PTL-DAT-016``, and an unordered or statistics-less one
earns the findings a vector asset would.

Needs the ``rashid[data]`` extra; skips without it. Fully local — no network.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")

from rashid.catalog import CatalogGraph  # noqa: E402
from rashid.data import (  # noqa: E402
    DAT_MIRROR,
    DAT_ORDERING,
    DAT_ROWGROUP_STATS,
    DAT_TABULAR,
    validate_data,
)
from rashid.model import Severity  # noqa: E402
from tests.conftest import CatalogBuilder, mutate_json  # noqa: E402
from tests.integration import _data_assets as assets  # noqa: E402

pytestmark = pytest.mark.integration

_PARQUET_TYPE = "application/vnd.apache.parquet"
_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_ITEM_IDS = ["scene-a", "scene-b"]
_MIRROR_KEY = "items-parquet"


def _cog_asset(href: str) -> dict[str, Any]:
    return {"href": href, "type": _COG_TYPE, "roles": ["data"]}


def _mirror_asset() -> dict[str, Any]:
    return {
        "href": "./items.parquet",
        "type": _PARQUET_TYPE,
        "roles": ["collection-mirror"],
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


def _build(root: Path, mirror_ids: list[str] | None) -> Path:
    """A raster scene collection with a mirror over ``mirror_ids``."""
    cat = CatalogBuilder(root)
    col = cat.collection("scenes", assets={_MIRROR_KEY: _mirror_asset()})
    for item_id in _ITEM_IDS:
        col.item(item_id, assets={"data": _cog_asset(f"./{item_id}.tif")})
    cat.write()

    scenes = root / "scenes"
    assets.write_item_mirror(scenes / "items.parquet", mirror_ids)
    for item_id in _ITEM_IDS:
        assets.write_cog(scenes / item_id / f"{item_id}.tif", size=256)
        _patch_checksum(scenes / item_id / f"{item_id}.json", scenes / item_id / f"{item_id}.tif")
    _patch_checksum(scenes / "collection.json", scenes / "items.parquet", _MIRROR_KEY)
    return root


@pytest.fixture(scope="module")
def matching(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("mirror") / "catalog", list(_ITEM_IDS))


@pytest.fixture
def catalog_root(matching: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "catalog"
    shutil.copytree(matching, dst)
    return dst


def _mirror_findings(root: Path) -> list:
    return [f for f in validate_data(CatalogGraph.load(root)) if f.rule_id == DAT_MIRROR]


def _rewrite_mirror(root: Path, ids: list[str] | None, **kwargs: Any) -> None:
    path = root / "scenes" / "items.parquet"
    assets.write_item_mirror(path, ids, **kwargs)
    _patch_checksum(root / "scenes" / "collection.json", path, _MIRROR_KEY)


def test_matching_mirror_is_clean(catalog_root: Path) -> None:
    findings = validate_data(CatalogGraph.load(catalog_root))
    assert findings == [], [f"{f.rule_id} {f.message}" for f in findings]


def test_unordered_mirror_is_flagged(catalog_root: Path) -> None:
    """PORTO-FMT-043: an item index is queried by extent like any other table.

    Six interleaved rows across three row groups, so ordering has something to
    judge. The row count no longer matches the collection's two items, which
    ``PTL-DAT-016`` reports separately; this test reads the storage rules only.
    """
    scattered = [f"scene-{n}" for n in range(6)]
    _rewrite_mirror(catalog_root, scattered, ordered=False, row_group_size=2)
    ids = {f.rule_id for f in validate_data(CatalogGraph.load(catalog_root))}
    assert DAT_ORDERING in ids


def test_mirror_without_row_group_statistics_is_flagged(catalog_root: Path) -> None:
    """The other half of PORTO-FMT-043: readers prune a mirror from metadata."""
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), covering=False)
    ids = {f.rule_id for f in validate_data(CatalogGraph.load(catalog_root))}
    assert DAT_ROWGROUP_STATS in ids


def test_mirror_is_not_held_to_the_tabular_shoulds(catalog_root: Path) -> None:
    """Those bind plain-Parquet assets with the 'data' role; a mirror has neither."""
    ids = {f.rule_id for f in validate_data(CatalogGraph.load(catalog_root))}
    assert DAT_TABULAR not in ids


def test_mirror_missing_an_item_is_flagged(catalog_root: Path) -> None:
    _rewrite_mirror(catalog_root, ["scene-a"])
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "scenes/collection.json"
    assert "1 item(s) absent" in findings[0].message
    assert "scene-b" in findings[0].message


def test_mirror_row_without_an_item_is_flagged(catalog_root: Path) -> None:
    _rewrite_mirror(catalog_root, [*_ITEM_IDS, "scene-c"])
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "1 mirror row(s) with no item" in findings[0].message
    assert "scene-c" in findings[0].message


def test_mirror_without_an_id_column_is_flagged(catalog_root: Path) -> None:
    _rewrite_mirror(catalog_root, None)
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "no 'id' column" in findings[0].message


def test_itemless_collection_mirror_is_not_compared(tmp_path: Path) -> None:
    """Nothing to compare against, so the check stays silent."""
    root = _build(tmp_path / "catalog", list(_ITEM_IDS))
    for item_id in _ITEM_IDS:
        shutil.rmtree(root / "scenes" / item_id)
    mutate_json(
        root / "scenes" / "collection.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "item"]),
    )
    assert _mirror_findings(root) == []
