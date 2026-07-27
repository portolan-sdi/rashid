from __future__ import annotations

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit


def test_spdx_identifier_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="MIT")
    assert findings_for(validate(catalog.write()), "PTL-LIC-001") == []


def test_missing_license(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("license"))
    findings = findings_for(validate(root), "PTL-LIC-001")
    assert len(findings) == 1
    assert "no license" in findings[0].message


def test_non_spdx_value(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="Apache 2.0")
    findings = findings_for(validate(catalog.write()), "PTL-LIC-001")
    assert len(findings) == 1


def test_case_insensitive_near_miss_gets_hint(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="apache-2.0")
    findings = findings_for(validate(catalog.write()), "PTL-LIC-001")
    assert len(findings) == 1
    assert findings[0].fix_hint is not None
    assert "Apache-2.0" in findings[0].fix_hint


def test_other_without_license_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="other")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-LIC-001") == []
    assert len(findings_for(report, "PTL-LIC-002")) == 1


def test_other_with_license_link_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="other")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d["links"].append(
            {
                "rel": "license",
                "href": "https://example.org/license.html",
                "type": "text/html",
            }
        ),
    )
    report = validate(root)
    assert findings_for(report, "PTL-LIC-002") == []


def test_proprietary_is_rejected(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="proprietary")
    report = validate(catalog.write())
    findings = findings_for(report, "PTL-LIC-003")
    assert len(findings) == 1
    assert findings[0].fix_hint is not None
    # LIC-001 defers to LIC-003 rather than double-reporting
    assert findings_for(report, "PTL-LIC-001") == []
