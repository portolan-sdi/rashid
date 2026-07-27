from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import CatalogBuilder, findings_for

pytestmark = pytest.mark.unit


def test_item_without_temporal_info_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024", properties={"datetime": None})
    findings = findings_for(validate(catalog.write()), "PTL-TMP-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING


def test_null_datetime_with_interval_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item(
        "roads-2024",
        properties={
            "datetime": None,
            "start_datetime": "2024-01-01T00:00:00Z",
            "end_datetime": "2024-12-31T23:59:59Z",
        },
    )
    report = validate(catalog.write())
    assert findings_for(report, "PTL-TMP-001") == []
    assert findings_for(report, "PTL-TMP-002") == []


def test_start_without_end_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item(
        "roads-2024",
        properties={"datetime": None, "start_datetime": "2024-01-01T00:00:00Z"},
    )
    report = validate(catalog.write())
    assert len(findings_for(report, "PTL-TMP-001")) == 1
    assert findings_for(report, "PTL-TMP-002") == []


def test_malformed_datetime_is_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024", properties={"datetime": "2024-13-01T00:00:00Z"})
    findings = findings_for(validate(catalog.write()), "PTL-TMP-002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_offsetless_datetime_is_rejected(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024", properties={"datetime": "2024-01-01T00:00:00"})
    assert len(findings_for(validate(catalog.write()), "PTL-TMP-002")) == 1


def test_start_after_end_is_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item(
        "roads-2024",
        properties={
            "datetime": None,
            "start_datetime": "2025-01-01T00:00:00Z",
            "end_datetime": "2024-01-01T00:00:00Z",
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-TMP-002")
    assert len(findings) == 1
    assert "after end_datetime" in findings[0].message


def test_valid_datetime_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-TMP-001") == []
    assert findings_for(report, "PTL-TMP-002") == []
