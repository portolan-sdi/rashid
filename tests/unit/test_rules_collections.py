"""Tests for PTL-COL-001: a single-file collection carries its data at collection level."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reis import validate
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
