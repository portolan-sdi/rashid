from __future__ import annotations

from typing import Any

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    CatalogBuilder,
    findings_for,
    mirror_providers,
    mutate_json,
    rule_ids,
)

pytestmark = pytest.mark.unit


def _mirror_collection(catalog: CatalogBuilder, **overrides: Any) -> None:
    collection = catalog.collection("census", providers=mirror_providers(), **overrides)
    collection.overrides.setdefault("updated", "2026-07-01T12:00:00Z")
    catalog.overrides.setdefault("updated", "2026-07-01T12:00:00Z")


def _add_mirror_links(root, with_canonical: bool = True) -> None:  # type: ignore[no-untyped-def]
    links = [{"rel": "via", "href": "https://source.example.org", "type": "text/html"}]
    if with_canonical:
        links.append(
            {
                "rel": "canonical",
                "href": "https://source.example.org/stac/catalog.json",
                "type": "application/json",
            }
        )
    mutate_json(root / "census" / "collection.json", lambda d: d["links"].extend(links))


def test_mirror_without_via_link(catalog: CatalogBuilder) -> None:
    _mirror_collection(catalog)
    root = catalog.write()
    findings = findings_for(validate(root), "PTL-PRO-001")
    assert len(findings) == 1
    assert "via" in findings[0].message


def test_mirror_via_link_with_wrong_type(catalog: CatalogBuilder) -> None:
    _mirror_collection(catalog)
    root = catalog.write()
    mutate_json(
        root / "census" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "via", "href": "https://source.example.org", "type": "application/json"}
        ),
    )
    findings = findings_for(validate(root), "PTL-PRO-001")
    assert len(findings) == 1
    assert "text/html" in findings[0].message


def test_mirror_without_canonical_is_info(catalog: CatalogBuilder) -> None:
    _mirror_collection(catalog)
    root = catalog.write()
    _add_mirror_links(root, with_canonical=False)
    findings = findings_for(validate(root), "PTL-PRO-002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO


def test_mirror_without_updated(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("census", providers=mirror_providers())
    catalog.overrides["updated"] = "2026-07-01T12:00:00Z"
    assert "updated" not in collection.overrides
    root = catalog.write()
    _add_mirror_links(root)
    findings = findings_for(validate(root), "PTL-PRO-003")
    assert len(findings) == 1
    assert findings[0].path == "census/collection.json"


def test_mirror_with_malformed_updated(catalog: CatalogBuilder) -> None:
    _mirror_collection(catalog, updated="July 1st, 2026")
    root = catalog.write()
    _add_mirror_links(root)
    findings = findings_for(validate(root), "PTL-PRO-003")
    assert len(findings) == 1
    assert "RFC 3339" in findings[0].message


def test_all_mirror_tree_requires_updated_on_root(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("census", providers=mirror_providers())
    collection.overrides["updated"] = "2026-07-01T12:00:00Z"
    root = catalog.write()
    _add_mirror_links(root)
    findings = findings_for(validate(root), "PTL-PRO-003")
    assert len(findings) == 1
    assert findings[0].path == "catalog.json"


def test_mixed_tree_does_not_require_updated_on_root(catalog: CatalogBuilder) -> None:
    _mirror_collection(catalog)
    catalog.overrides.pop("updated", None)
    official = catalog.collection("roads")
    assert official is not None
    root = catalog.write()
    _add_mirror_links(root)
    assert findings_for(validate(root), "PTL-PRO-003") == []


def test_official_with_via_link_is_rejected(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "via", "href": "https://elsewhere.example.org", "type": "text/html"}
        ),
    )
    findings = findings_for(validate(root), "PTL-PRO-004")
    assert len(findings) == 1
    assert "official" in findings[0].message


def test_broken_providers_silence_provenance_rules(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        providers=[{"name": "Demo Org", "roles": ["producer"]}],  # no host
    )
    report = validate(catalog.write())
    assert "PTL-PRV-002" in rule_ids(report)
    assert not rule_ids(report) & {
        "PTL-PRO-001",
        "PTL-PRO-002",
        "PTL-PRO-003",
        "PTL-PRO-004",
    }
