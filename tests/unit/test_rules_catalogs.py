"""Tests for the PTL-CAT rules: subcatalog fan-out."""

from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import CatalogBuilder, findings_for

pytestmark = pytest.mark.unit


def test_nineteen_collections_are_clean(catalog: CatalogBuilder) -> None:
    for index in range(19):
        catalog.collection(f"layer-{index:02d}")
    assert findings_for(validate(catalog.write()), "PTL-CAT-001") == []


def test_twenty_collections_are_flagged(catalog: CatalogBuilder) -> None:
    for index in range(20):
        catalog.collection(f"layer-{index:02d}")
    findings = findings_for(validate(catalog.write()), "PTL-CAT-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert findings[0].path == "catalog.json"
    assert "20 children" in findings[0].message


def test_twenty_subcatalogs_are_clean(catalog: CatalogBuilder) -> None:
    for index in range(20):
        catalog.subcatalog(f"theme-{index:02d}").collection(f"layer-{index:02d}")
    assert findings_for(validate(catalog.write()), "PTL-CAT-001") == []


def test_grouped_catalog_with_a_flat_tail_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.subcatalog("environment").collection("air-quality")
    for index in range(20):
        catalog.collection(f"layer-{index:02d}")
    findings = findings_for(validate(catalog.write()), "PTL-CAT-001")
    assert len(findings) == 1
    assert findings[0].path == "catalog.json"


def test_a_collections_items_do_not_count_for_its_catalog(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("scenes")
    for index in range(20):
        collection.item(f"scene-{index:02d}")
    assert findings_for(validate(catalog.write()), "PTL-CAT-001") == []
