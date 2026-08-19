"""Data pass over an item mirror: it must reproduce the items, storage rules bind.

A mirror is a GeoParquet file like any other, so the storage rules apply to it
(PORTO-FMT-043) on top of the agreement check that is its own (PORTO-FMT-042).
These tests cover both halves: a conformant mirror is clean, one that diverges
in row count, ids, geometry, datetime, or bbox earns ``PTL-DAT-016``, and an
unordered or statistics-less one earns the findings a vector asset would.

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

from rashid import validate  # noqa: E402
from rashid.catalog import CatalogGraph  # noqa: E402
from rashid.data import (  # noqa: E402
    DAT_MIRROR,
    DAT_ORDERING,
    DAT_ROWGROUP_STATS,
    DAT_TABULAR,
    validate_data,
)
from rashid.model import Severity  # noqa: E402
from tests.conftest import (  # noqa: E402
    CatalogBuilder,
    mutate_json,
    write_organizing_catalog_layout,
)
from tests.integration import _data_assets as assets  # noqa: E402

pytestmark = pytest.mark.integration

_PARQUET_TYPE = "application/vnd.apache.parquet"
_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_ITEM_IDS = ["scene-a", "scene-b"]
_MIRROR_KEY = "items-parquet"
# Each item's footprint and instant, mirrored row for row. The points are the
# two ends of the bbox diagonal, so the rows stay spatially ordered.
_ITEM_FIELDS = {
    "scene-a": ((4.0, 50.0), "2024-01-01T00:00:00Z"),
    "scene-b": ((6.0, 52.0), "2024-06-01T12:30:00Z"),
}
_FILLER_DATETIME = "2024-03-01T00:00:00Z"


def _cog_asset(href: str) -> dict[str, Any]:
    return {"href": href, "type": _COG_TYPE, "roles": ["data"]}


def _item_fields(item_id: str) -> dict[str, Any]:
    """The geometry, bbox, and datetime the mirror has to reproduce."""
    (x, y), stamp = _ITEM_FIELDS[item_id]
    return {
        "geometry": {"type": "Point", "coordinates": [x, y]},
        "bbox": [x, y, x, y],
        "properties": {"datetime": stamp},
    }


def _rows_for(ids: list[str], *, ordered: bool = True) -> dict[str, Any]:
    """Mirror columns that agree with every id that names a real item.

    An id with no item is filled from the ordering source, since nothing
    compares it; that is what lets the storage-rule tests scatter rows.
    """
    source = assets.ordered_points(max(len(ids), 2)) if ordered else assets.interleaved_points()
    points = []
    datetimes = []
    for index, item_id in enumerate(ids):
        point, stamp = _ITEM_FIELDS.get(item_id, (source[index], _FILLER_DATETIME))
        points.append(point)
        datetimes.append(stamp)
    return {
        "points": points,
        "datetimes": datetimes,
        "bboxes": [(x, y, x, y) for x, y in points],
    }


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
        col.item(item_id, assets={"data": _cog_asset(f"./{item_id}.tif")}, **_item_fields(item_id))
    cat.write()

    scenes = root / "scenes"
    rows = _rows_for(mirror_ids) if mirror_ids else {}
    assets.write_item_mirror(scenes / "items.parquet", mirror_ids, **rows)
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
    """Rewrite the mirror, agreeing with the items except where told otherwise."""
    path = root / "scenes" / "items.parquet"
    if ids is not None:
        for field, value in _rows_for(ids, ordered=kwargs.get("ordered", True)).items():
            kwargs.setdefault(field, value)
    assets.write_item_mirror(path, ids, **kwargs)
    _patch_checksum(root / "scenes" / "collection.json", path, _MIRROR_KEY)


def test_matching_mirror_is_clean(catalog_root: Path) -> None:
    findings = validate_data(CatalogGraph.load(catalog_root))
    assert findings == [], [f"{f.rule_id} {f.message}" for f in findings]


def test_unordered_mirror_is_flagged(catalog_root: Path) -> None:
    """PORTO-FMT-043: an item index is queried by extent like any other table.

    Ten interleaved rows across five row groups, the count at which
    PORTO-FMT-006's criteria start applying. The row count no longer matches
    the collection's two items, which ``PTL-DAT-016`` reports separately; this
    test reads the storage rules only.
    """
    scattered = [f"scene-{n}" for n in range(10)]
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
    assert all(f.severity is Severity.ERROR for f in findings)
    assert all(f.path == "scenes/collection.json" for f in findings)
    messages = [f.message for f in findings]
    assert any("1 item(s) absent" in m and "scene-b" in m for m in messages)
    assert any("holds 1 row(s) for 2 item(s)" in m for m in messages)


def test_mirror_row_without_an_item_is_flagged(catalog_root: Path) -> None:
    _rewrite_mirror(catalog_root, [*_ITEM_IDS, "scene-c"])
    messages = [f.message for f in _mirror_findings(catalog_root)]
    assert any("1 mirror row(s) with no item" in m and "scene-c" in m for m in messages)


def test_duplicate_rows_for_one_item_are_flagged(catalog_root: Path) -> None:
    """The id sets still match, so only the row count catches this."""
    _rewrite_mirror(catalog_root, ["scene-a", *_ITEM_IDS])
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "holds 3 row(s) for 2 item(s)" in findings[0].message
    assert "one row per item" in findings[0].message


def test_mirror_geometry_drift_is_flagged(catalog_root: Path) -> None:
    """One row's footprint moves; its id, bbox, and datetime stay put."""
    moved = [_ITEM_FIELDS["scene-a"][0], (5.5, 51.5)]
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), points=moved)
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "geometry disagrees with the item it names for 1 item(s)" in findings[0].message
    assert "scene-b" in findings[0].message


def test_geometry_drift_below_the_tolerance_is_clean(catalog_root: Path) -> None:
    """A coordinate written to six decimals rounds by less than the tolerance."""
    (x, y) = _ITEM_FIELDS["scene-b"][0]
    nudged = [_ITEM_FIELDS["scene-a"][0], (x + 4e-7, y - 4e-7)]
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), points=nudged)
    assert _mirror_findings(catalog_root) == []


def test_mirror_datetime_drift_is_flagged(catalog_root: Path) -> None:
    stamps = [_ITEM_FIELDS["scene-a"][1], "2024-06-02T12:30:00Z"]
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), datetimes=stamps)
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "datetime disagrees with the item it names for 1 item(s)" in findings[0].message
    assert "scene-b" in findings[0].message


def test_datetime_written_in_another_offset_is_clean(catalog_root: Path) -> None:
    """Same instant, different offset: the comparison normalizes to UTC."""
    stamps = [_ITEM_FIELDS["scene-a"][1], "2024-06-01T14:30:00+02:00"]
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), datetimes=stamps)
    assert _mirror_findings(catalog_root) == []


def test_mirror_without_a_datetime_column_is_flagged(catalog_root: Path) -> None:
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), datetimes=None)
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "no 'datetime' column" in findings[0].message


def test_mirror_bbox_drift_is_flagged(catalog_root: Path) -> None:
    """The covering box moves while the geometry it covers does not."""
    (ax, ay), (bx, by) = (_ITEM_FIELDS[item_id][0] for item_id in _ITEM_IDS)
    drifted = [(ax, ay, ax, ay), (bx - 0.5, by - 0.5, bx, by)]
    _rewrite_mirror(catalog_root, list(_ITEM_IDS), bboxes=drifted)
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "bbox disagrees with the item it names for 1 item(s)" in findings[0].message
    assert "scene-b" in findings[0].message


def test_mirror_without_an_id_column_is_flagged(catalog_root: Path) -> None:
    _rewrite_mirror(catalog_root, None)
    findings = _mirror_findings(catalog_root)
    assert len(findings) == 1
    assert "no 'id' column" in findings[0].message


def _nested_layout(catalog: CatalogBuilder, stamp: str) -> Path:
    """The catalog-under-collection tree, with a mirror over its one item.

    core.md, Core Structure allows a catalog below a collection to organize
    its items, which puts the item two levels under the collection that owns
    it. ``write_organizing_catalog_layout`` leaves that item at the builder's
    defaults, so the mirror reproduces those.
    """
    root = write_organizing_catalog_layout(catalog)
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d["assets"].__setitem__(_MIRROR_KEY, _mirror_asset()),
    )
    path = root / "roads" / "items.parquet"
    assets.write_item_mirror(
        path,
        ["roads-2024"],
        points=[(5.0, 51.0)],
        datetimes=[stamp],
        bboxes=[(4.0, 50.0, 6.0, 52.0)],
    )
    _patch_checksum(root / "roads" / "collection.json", path, _MIRROR_KEY)
    return root


def test_items_under_an_organizing_catalog_are_compared(catalog: CatalogBuilder) -> None:
    """A mirror over nested items is verified, not skipped for want of children."""
    root = _nested_layout(catalog, "2024-01-01T00:00:00Z")
    assert _mirror_findings(root) == []


def test_drift_under_an_organizing_catalog_is_flagged(catalog: CatalogBuilder) -> None:
    """The same tree with one field moved: the check has something to say."""
    root = _nested_layout(catalog, "2024-05-05T00:00:00Z")
    findings = _mirror_findings(root)
    assert len(findings) == 1
    assert "datetime disagrees with the item it names" in findings[0].message
    assert "roads-2024" in findings[0].message


def test_itemless_collection_is_left_to_the_metadata_pass(tmp_path: Path) -> None:
    """A mirror over zero items is a structural defect, so PTL-COL-005 owns it.

    The data pass stands down rather than report the same fault a second time
    in row-level terms. PTL-COL-005 reports it from metadata alone, which is
    what makes the case survive a missing ``data`` extra.
    """
    root = _build(tmp_path / "catalog", list(_ITEM_IDS))
    for item_id in _ITEM_IDS:
        shutil.rmtree(root / "scenes" / item_id)
    mutate_json(
        root / "scenes" / "collection.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "item"]),
    )
    assert _mirror_findings(root) == []
    structural = [f for f in validate(root).findings if f.rule_id == "PTL-COL-005"]
    assert len(structural) == 1
    assert "publishes no items" in structural[0].message
