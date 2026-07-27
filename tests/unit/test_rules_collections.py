"""Tests for the PTL-COL rules: single-file shape, no nesting, ID conventions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reis import validate
from reis.model import Severity
from tests.conftest import CatalogBuilder, default_asset, findings_for, mutate_json

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
