from __future__ import annotations

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit


def test_no_providers(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("providers"))
    report = validate(root)
    assert len(findings_for(report, "PTL-PRV-001")) == 1
    # PRV-002/003 stay silent instead of cascading
    assert findings_for(report, "PTL-PRV-002") == []
    assert findings_for(report, "PTL-PRV-003") == []


def test_no_producer_role(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[{"name": "Demo Org", "roles": ["host"], "url": "https://example.org"}],
    )
    findings = findings_for(validate(catalog.write()), "PTL-PRV-001")
    assert len(findings) == 1
    assert "producer" in findings[0].message


def test_zero_hosts(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[{"name": "Demo Org", "roles": ["producer"], "url": "https://example.org"}],
    )
    findings = findings_for(validate(catalog.write()), "PTL-PRV-002")
    assert len(findings) == 1
    assert "found 0" in findings[0].message


def test_two_hosts(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[
            {"name": "A", "roles": ["producer", "host"], "url": "https://a.example.org"},
            {"name": "B", "roles": ["host"], "url": "https://b.example.org"},
        ],
    )
    findings = findings_for(validate(catalog.write()), "PTL-PRV-002")
    assert len(findings) == 1
    assert "found 2" in findings[0].message


def test_host_not_last(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[
            {"name": "Host Org", "roles": ["host"], "url": "https://h.example.org"},
            {"name": "Maker Org", "roles": ["producer"]},
        ],
    )
    findings = findings_for(validate(catalog.write()), "PTL-PRV-002")
    assert len(findings) == 1
    assert "last element" in findings[0].message


def test_host_without_contact(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[{"name": "Demo Org", "roles": ["producer", "host"]}],
    )
    findings = findings_for(validate(catalog.write()), "PTL-PRV-003")
    assert len(findings) == 1
    assert "neither a url nor an email" in findings[0].message


def test_host_with_email_only_passes(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[{"name": "Demo Org", "roles": ["producer", "host"], "email": "gis@example.org"}],
    )
    assert findings_for(validate(catalog.write()), "PTL-PRV-003") == []
