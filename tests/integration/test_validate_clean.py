"""The golden-path guard: a fully valid catalog yields zero findings."""

from __future__ import annotations

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, mirror_providers


@pytest.mark.integration
def test_minimal_catalog_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", title="Road Centerlines")
    root = catalog.write()
    report = validate(root)
    assert report.findings == []
    assert report.passed
    assert report.files_checked == 2


@pytest.mark.integration
def test_catalog_with_items_and_subcatalog_is_clean(catalog: CatalogBuilder) -> None:
    roads = catalog.collection("roads", title="Road Centerlines")
    roads.item("roads-2024")
    env = catalog.subcatalog("environment", title="Environment")
    env.collection("air-quality", title="Air Quality Measurements")
    root = catalog.write()
    report = validate(root)
    assert report.findings == []
    assert report.files_checked == 5


@pytest.mark.integration
def test_mirror_collection_is_clean(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("census", title="Census Tracts")
    collection.overrides["providers"] = mirror_providers()
    collection.overrides["updated"] = "2026-07-01T12:00:00Z"
    # every collection in this tree is a mirror, so the root also records sync time
    catalog.overrides["updated"] = "2026-07-01T12:00:00Z"
    root = catalog.write()
    mirror_links = [
        {"rel": "via", "href": "https://source.example.org/census", "type": "text/html"},
        {
            "rel": "canonical",
            "href": "https://source.example.org/stac/catalog.json",
            "type": "application/json",
        },
    ]
    from tests.conftest import mutate_json

    mutate_json(
        root / "census" / "collection.json",
        lambda data: data["links"].extend(mirror_links),
    )
    report = validate(root)
    assert report.findings == []
