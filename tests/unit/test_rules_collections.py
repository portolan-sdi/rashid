"""Tests for the PTL-COL rules: single-file shape, raster scenes, no nesting, IDs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    CatalogBuilder,
    default_asset,
    findings_for,
    mutate_json,
    nest_items_under_organizing_catalog,
    write_language_trees,
)

pytestmark = pytest.mark.unit


def _collection(root: Path) -> Path:
    return root / "roads" / "collection.json"


def _drop_collection_data(root: Path) -> None:
    """Strip the builder's collection-level data asset, leaving the item's."""
    mutate_json(_collection(root), lambda d: d["assets"].pop("data", None))


def test_collection_level_data_asset_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-COL-001") == []


def test_itemless_collection_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-COL-001") == []


def test_lone_item_wrapping_the_only_data_file_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    _drop_collection_data(root)
    findings = findings_for(validate(root), "PTL-COL-001")
    assert len(findings) == 1
    assert "seg1" in findings[0].message
    assert findings[0].path == "roads/collection.json"


def test_multi_item_collection_is_out_of_scope(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("seg1")
    collection.item("seg2")
    root = catalog.write()
    _drop_collection_data(root)
    assert findings_for(validate(root), "PTL-COL-001") == []


def test_lone_item_with_several_data_files_is_out_of_scope(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    _drop_collection_data(root)

    def add_second_data_asset(data: dict[str, Any]) -> None:
        second = default_asset()
        second["href"] = "./data-b.parquet"
        data["assets"]["data-b"] = second

    mutate_json(root / "roads" / "seg1" / "seg1.json", add_second_data_asset)
    assert findings_for(validate(root), "PTL-COL-001") == []


def test_item_without_a_data_asset_is_out_of_scope(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    _drop_collection_data(root)
    mutate_json(
        root / "roads" / "seg1" / "seg1.json",
        lambda d: d["assets"]["data"].__setitem__("roles", ["metadata"]),
    )
    assert findings_for(validate(root), "PTL-COL-001") == []


def test_partitioned_collection_may_model_its_partition_as_an_item(
    catalog: CatalogBuilder,
) -> None:
    catalog.collection("roads").item("part-0")
    root = catalog.write()
    _drop_collection_data(root)

    def partition(data: dict[str, Any]) -> None:
        data["partition:scheme"] = "hive"
        data["description"] = "Roads partitioned at s3://bucket/roads/*.parquet"

    mutate_json(_collection(root), partition)
    assert findings_for(validate(root), "PTL-COL-001") == []


def test_roleless_collection_asset_makes_the_shape_undecidable(catalog: CatalogBuilder) -> None:
    """Roles identify the data asset; without them PTL-AST-001 owns the finding."""
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    mutate_json(_collection(root), lambda d: d["assets"]["data"].pop("roles"))
    report = validate(root)
    assert findings_for(report, "PTL-COL-001") == []
    assert findings_for(report, "PTL-AST-001") != []


def test_collection_data_asset_alongside_an_item_is_clean(catalog: CatalogBuilder) -> None:
    """Data already exposed at collection level satisfies the MUST."""
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    assert findings_for(validate(root), "PTL-COL-001") == []


# --- PTL-COL-004: raster scenes belong on items -----------------------------

_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def _cog_asset(href: str) -> dict[str, Any]:
    asset = default_asset()
    asset["href"] = href
    asset["type"] = _COG_TYPE
    return asset


def _make_cog(path: Path, key: str = "data", href: str = "./scene.tif") -> None:
    """Turn a node's data asset into a COG asset."""
    mutate_json(path, lambda d: d["assets"].__setitem__(key, _cog_asset(href)))


def _item_json(root: Path, item_id: str) -> Path:
    return root / "roads" / item_id / f"{item_id}.json"


def test_single_collection_level_cog_is_clean(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-072's prescribed shape: one COG, no item directory."""
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root))
    assert findings_for(validate(root), "PTL-COL-004") == []


def test_multiple_collection_level_cogs_are_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root), href="./scene-a.tif")
    mutate_json(
        _collection(root),
        lambda d: d["assets"].__setitem__("scene-b", _cog_asset("./scene-b.tif")),
    )
    findings = findings_for(validate(root), "PTL-COL-004")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert "2 scene COGs" in findings[0].message


def test_wrong_cog_types_do_not_hide_collection_level_scenes(
    catalog: CatalogBuilder,
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root), href="./scene-a.tif")
    mutate_json(
        _collection(root),
        lambda d: (
            d["assets"]["data"].__setitem__("type", "image/tiff; application=geotiff"),
            d["assets"].__setitem__(
                "scene-b",
                {
                    **_cog_asset("./scene-b.tiff"),
                    "type": "image/tiff; application=geotiff",
                },
            ),
        ),
    )
    report = validate(root)
    findings = findings_for(report, "PTL-COL-004")
    assert len(findings) == 1
    assert "2 scene COGs" in findings[0].message
    assert len(findings_for(report, "PTL-AST-006")) == 2


def test_scenes_modelled_as_items_are_clean(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("scene-a")
    collection.item("scene-b")
    root = catalog.write()
    _drop_collection_data(root)
    _make_cog(_item_json(root, "scene-a"), href="./scene-a.tif")
    _make_cog(_item_json(root, "scene-b"), href="./scene-b.tif")
    assert findings_for(validate(root), "PTL-COL-004") == []


def test_collection_level_cog_alongside_scene_items_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("scene-b")
    root = catalog.write()
    _make_cog(_collection(root), href="./scene-a.tif")
    _make_cog(_item_json(root, "scene-b"), href="./scene-b.tif")
    findings = findings_for(validate(root), "PTL-COL-004")
    assert len(findings) == 1
    assert "scene-b" in findings[0].message


def test_lone_item_wrapping_a_cog_is_left_to_the_single_file_rule(
    catalog: CatalogBuilder,
) -> None:
    """The complementary shape is PTL-COL-001's; the two never double-report."""
    catalog.collection("roads").item("scene-a")
    root = catalog.write()
    _drop_collection_data(root)
    _make_cog(_item_json(root, "scene-a"), href="./scene-a.tif")
    report = validate(root)
    assert findings_for(report, "PTL-COL-004") == []
    assert len(findings_for(report, "PTL-COL-001")) == 1


def test_several_non_raster_collection_assets_are_out_of_scope(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    def add_second_parquet(data: dict[str, Any]) -> None:
        second = default_asset()
        second["href"] = "./data-b.parquet"
        data["assets"]["data-b"] = second

    mutate_json(_collection(root), add_second_parquet)
    assert findings_for(validate(root), "PTL-COL-004") == []


def test_upstream_geotiff_original_is_not_a_scene(catalog: CatalogBuilder) -> None:
    """A plain GeoTIFF carrying the 'source' role is provenance, not a scene."""
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root), href="./scene-a.tif")

    def add_source_original(data: dict[str, Any]) -> None:
        asset = _cog_asset("https://example.org/original.tif")
        asset["type"] = "image/tiff"
        asset["roles"] = ["data", "source"]
        data["assets"]["source"] = asset

    mutate_json(_collection(root), add_source_original)
    report = validate(root)
    assert findings_for(report, "PTL-COL-004") == []
    assert findings_for(report, "PTL-AST-006") == []


def test_roleless_raster_assets_are_out_of_scope(catalog: CatalogBuilder) -> None:
    """Roles identify the data asset; PTL-AST-001 owns the missing-roles finding."""
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root), href="./scene-a.tif")

    def add_roleless_raster(data: dict[str, Any]) -> None:
        asset = _cog_asset("./scene-b.tif")
        asset.pop("roles")
        data["assets"]["scene-b"] = asset

    mutate_json(_collection(root), add_roleless_raster)
    report = validate(root)
    assert findings_for(report, "PTL-COL-004") == []
    assert findings_for(report, "PTL-AST-001") != []


# --- PTL-COL-005: the item tree a collection owes ---------------------------


_MIRROR_ASSET = {
    "href": "./items.parquet",
    "type": "application/vnd.apache.parquet",
    "title": "Item mirror",
    "roles": ["collection-mirror"],
}


def _write_scenes(collection_dir: Path, *names: str) -> None:
    """Put scene files in the collection directory. The bytes are never read."""
    for name in names:
        (collection_dir / name).write_bytes(b"II*\x00")


def test_scene_files_without_items_are_flagged(catalog: CatalogBuilder) -> None:
    """The GHSL shape: scene files on disk, nothing in the metadata about them."""
    catalog.collection("roads")
    root = catalog.write()
    _write_scenes(root / "roads", "pop-1975.tif", "pop-1990.tif", "pop-2000.tif")
    findings = findings_for(validate(root), "PTL-COL-005")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert findings[0].json_pointer == "/links"
    assert "3 raster scene file(s)" in findings[0].message
    assert "publishes no items" in findings[0].message
    for spec_id in ("PORTO-CORE-015", "PORTO-CORE-032", "PORTO-CORE-071"):
        assert spec_id in findings[0].message


def test_scene_file_names_are_truncated(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _write_scenes(root / "roads", *[f"pop-{year}.tif" for year in range(2000, 2006)])
    message = findings_for(validate(root), "PTL-COL-005")[0].message
    assert "pop-2000.tif, pop-2001.tif, pop-2002.tif, and 3 more" in message


def test_one_scene_file_is_out_of_scope(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-071 binds several scenes; one stray file is not that."""
    catalog.collection("roads")
    root = catalog.write()
    _write_scenes(root / "roads", "scene.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_declared_collection_level_cogs_are_left_to_the_scene_rule(
    catalog: CatalogBuilder,
) -> None:
    """Declared scene COGs are PTL-COL-004's; the two never both report."""
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root), href="./scene-a.tif")
    mutate_json(
        _collection(root),
        lambda d: d["assets"].__setitem__("scene-b", _cog_asset("./scene-b.tif")),
    )
    _write_scenes(root / "roads", "scene-a.tif", "scene-b.tif")
    report = validate(root)
    assert findings_for(report, "PTL-COL-005") == []
    assert len(findings_for(report, "PTL-COL-004")) == 1


def test_declared_source_geotiffs_are_not_scenes(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-035 lets an upstream GeoTIFF stay; a declared file is exempt."""
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        _collection(root),
        lambda d: d["assets"].update(
            {
                "source-a": {
                    "href": "./upstream-a.tif",
                    "type": "image/tiff",
                    "title": "Upstream A",
                    "roles": ["source"],
                },
                "source-b": {
                    "href": "./upstream-b.tif",
                    "type": "image/tiff",
                    "title": "Upstream B",
                    "roles": ["source"],
                },
            }
        ),
    )
    _write_scenes(root / "roads", "upstream-a.tif", "upstream-b.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_single_collection_level_cog_on_disk_is_clean(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-072's shape, with the file actually present."""
    catalog.collection("roads")
    root = catalog.write()
    _make_cog(_collection(root))
    _write_scenes(root / "roads", "scene.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_scene_items_alongside_stray_files_are_clean(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("scene-a")
    collection.item("scene-b")
    root = catalog.write()
    _drop_collection_data(root)
    _make_cog(_item_json(root, "scene-a"), href="./scene-a.tif")
    _make_cog(_item_json(root, "scene-b"), href="./scene-b.tif")
    _write_scenes(root / "roads", "leftover-a.tif", "leftover-b.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_items_under_an_organizing_catalog_are_clean(catalog: CatalogBuilder) -> None:
    """Containment moves, ownership does not: items_of still finds them."""
    collection = catalog.collection("roads")
    collection.item("scene-a")
    collection.item("scene-b")
    root = catalog.write()
    nest_items_under_organizing_catalog(root, root / "roads")
    _write_scenes(root / "roads", "scene-a.tif", "scene-b.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_partitioned_collection_with_raster_parts_is_clean(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-021 lets a partitioned collection leave its parts off items."""
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        _collection(root),
        lambda d: d.update(
            {
                "partition:scheme": "hive",
                "partition:fields": ["year"],
                "partition:count": 2,
            }
        ),
    )
    _write_scenes(root / "roads", "year=2024.tif", "year=2025.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_registered_mirror_without_items_is_flagged(catalog: CatalogBuilder) -> None:
    """The mirror is a claim that items exist, and it stands on its own."""
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(_collection(root), lambda d: d["assets"].__setitem__("mirror", _MIRROR_ASSET))
    findings = findings_for(validate(root), "PTL-COL-005")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/assets/mirror"
    assert "registers item mirror 'mirror'" in findings[0].message
    assert "PORTO-FMT-042" in findings[0].message


def test_mirror_with_items_is_clean(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("scene-a")
    collection.item("scene-b")
    root = catalog.write()
    mutate_json(_collection(root), lambda d: d["assets"].__setitem__("mirror", _MIRROR_ASSET))
    assert findings_for(validate(root), "PTL-COL-005") == []


def test_itemless_collection_without_scenes_or_mirror_is_clean(
    catalog: CatalogBuilder,
) -> None:
    """A single-file collection owes no items (PORTO-CORE-017)."""
    catalog.collection("roads")
    assert findings_for(validate(catalog.write()), "PTL-COL-005") == []


def test_scene_files_and_a_mirror_report_once(catalog: CatalogBuilder) -> None:
    """The GHSL collection showed both signals; the disk one is the report."""
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(_collection(root), lambda d: d["assets"].__setitem__("mirror", _MIRROR_ASSET))
    _write_scenes(root / "roads", "pop-1975.tif", "pop-1990.tif")
    findings = findings_for(validate(root), "PTL-COL-005")
    assert len(findings) == 1
    assert "raster scene file(s)" in findings[0].message


def test_scene_files_in_one_language_tree_do_not_fault_the_other(
    catalog: CatalogBuilder,
) -> None:
    collection = catalog.collection("roads")
    collection.item("scene-a")
    collection.item("scene-b")
    root = write_language_trees(catalog, "fr", "roads")
    _write_scenes(root / "roads", "scene-a.tif", "scene-b.tif")
    assert findings_for(validate(root), "PTL-COL-005") == []


# --- PTL-COL-002: no nested collections ------------------------------------


def _write_nested_collection(root: Path, *parts: str) -> None:
    """Drop a minimal collection.json at root/<parts>/collection.json."""
    directory = root.joinpath(*parts)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "collection.json").write_text(
        json.dumps({"type": "Collection", "stac_version": "1.1.0", "id": parts[-1]}),
        encoding="utf-8",
    )


def test_flat_collections_are_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    catalog.subcatalog("regions").collection("rivers")
    assert findings_for(validate(catalog.write()), "PTL-COL-002") == []


def test_collection_inside_a_collection_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _write_nested_collection(root, "roads", "segments")
    findings = findings_for(validate(root), "PTL-COL-002")
    assert len(findings) == 1
    assert findings[0].path == "roads/segments/collection.json"
    assert "'roads'" in findings[0].message


def test_collection_below_an_item_organizing_catalog_is_flagged(catalog: CatalogBuilder) -> None:
    # A catalog below a collection is legal; a collection below that catalog
    # still nests inside the outer collection and is not.
    catalog.collection("roads")
    root = catalog.write()
    scenes = root / "roads" / "scenes"
    scenes.mkdir(parents=True)
    (scenes / "catalog.json").write_text(
        json.dumps({"type": "Catalog", "stac_version": "1.1.0", "id": "scenes"}),
        encoding="utf-8",
    )
    _write_nested_collection(root, "roads", "scenes", "deep")
    findings = findings_for(validate(root), "PTL-COL-002")
    assert len(findings) == 1
    assert findings[0].path == "roads/scenes/deep/collection.json"


def test_catalog_below_a_collection_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    scenes = root / "roads" / "scenes"
    scenes.mkdir(parents=True)
    (scenes / "catalog.json").write_text(
        json.dumps({"type": "Catalog", "stac_version": "1.1.0", "id": "scenes"}),
        encoding="utf-8",
    )
    assert findings_for(validate(root), "PTL-COL-002") == []


# --- PTL-COL-003: collection ID conventions ---------------------------------


def test_conformant_ids_are_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    catalog.collection("air-quality_2024")
    assert findings_for(validate(catalog.write()), "PTL-COL-003") == []


def test_nested_posix_path_id_is_clean(catalog: CatalogBuilder) -> None:
    catalog.subcatalog("environment").collection("air-quality")
    root = catalog.write()
    mutate_json(
        root / "environment" / "air-quality" / "collection.json",
        lambda d: d.__setitem__("id", "environment/air-quality"),
    )
    assert findings_for(validate(root), "PTL-COL-003") == []


@pytest.mark.parametrize("bad_id", ["Roads", "9roads", "roads.2024", "-roads", "ro ads"])
def test_nonconformant_id_is_a_warning(catalog: CatalogBuilder, bad_id: str) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.__setitem__("id", bad_id))
    findings = findings_for(validate(root), "PTL-COL-003")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert bad_id in findings[0].message


def test_duplicate_id_is_flagged_once_on_the_later_collection(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    catalog.collection("rivers")
    root = catalog.write()
    mutate_json(root / "rivers" / "collection.json", lambda d: d.__setitem__("id", "roads"))
    findings = findings_for(validate(root), "PTL-COL-003")
    assert len(findings) == 1
    # flagged on the later collection in path order ("rivers/" sorts first)
    assert findings[0].path == "roads/collection.json"
    assert "not unique" in findings[0].message


def test_a_collection_keeps_its_id_in_every_language(catalog: CatalogBuilder) -> None:
    """core.md, Alternate-Language Trees: one object repeated across trees keeps one ID.

    The shared ID is what lets a client match a collection to its translations,
    so reporting it as a duplicate would push publishers into breaking the match.
    """
    report = validate(write_language_trees(catalog))
    assert findings_for(report, "PTL-COL-003") == []


def test_duplicate_ids_inside_one_language_tree_are_still_reported(
    catalog: CatalogBuilder,
) -> None:
    """The carve-out reaches across trees only, never within one."""
    catalog.subcatalog("regional").collection("roads")
    root = write_language_trees(catalog)
    findings = findings_for(validate(root), "PTL-COL-003")
    assert len(findings) == 1
    assert findings[0].path == "roads/collection.json"
    assert "regional/roads/collection.json" in findings[0].message
