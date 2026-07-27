from __future__ import annotations

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit


def _set_collection_bbox(root, bbox) -> None:  # type: ignore[no-untyped-def]
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d["extent"]["spatial"].__setitem__("bbox", [bbox]),
    )


def test_longitude_out_of_range(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [4.0, 50.0, 190.0, 52.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "east longitude 190.0" in findings[0].message


def test_south_greater_than_north(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [4.0, 52.0, 6.0, 50.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "south" in findings[0].message


def test_sentinel_value(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [-1.7976931348623157e308, 50.0, 6.0, 52.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "sentinel" in findings[0].message


def test_literal_nan(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [float("nan"), 50.0, 6.0, 52.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "NaN or infinite" in findings[0].message


def test_literal_infinity_in_item_bbox(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "roads-2024" / "roads-2024.json",
        lambda d: d.__setitem__("bbox", [4.0, 50.0, float("inf"), 52.0]),
    )
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert findings[0].path == "roads/roads-2024/roads-2024.json"


def test_antimeridian_crossing_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [170.0, -10.0, -170.0, 10.0])
    assert findings_for(validate(root), "PTL-BBX-001") == []


def test_3d_bbox_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [4.0, 50.0, 0.0, 6.0, 52.0, 3500.0])
    assert findings_for(validate(root), "PTL-BBX-001") == []


def test_3d_bbox_inverted_elevation(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [4.0, 50.0, 3500.0, 6.0, 52.0, 0.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "elevation" in findings[0].message


def test_wrong_length_bbox(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [4.0, 50.0, 6.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "4 or 6 numbers" in findings[0].message


def test_non_numeric_bbox_value(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _set_collection_bbox(root, [4.0, "50", 6.0, 52.0])
    findings = findings_for(validate(root), "PTL-BBX-001")
    assert len(findings) == 1
    assert "non-numeric" in findings[0].message
