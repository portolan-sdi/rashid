from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit


def test_missing_title(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(root / "catalog.json", lambda d: d.pop("title"))
    findings = findings_for(validate(root), "PTL-TTL-001")
    assert len(findings) == 1
    assert "'title'" in findings[0].message


def test_empty_description(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.__setitem__("description", "  "))
    findings = findings_for(validate(root), "PTL-TTL-001")
    assert len(findings) == 1
    assert findings[0].path == "roads/collection.json"


def test_slug_title_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", title="road_centerlines_2024")
    findings = findings_for(validate(catalog.write()), "PTL-TTL-002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "raw slug" in findings[0].message


def test_namespaced_title_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("addresses", title="os:Address")
    findings = findings_for(validate(catalog.write()), "PTL-TTL-002")
    assert len(findings) == 1
    assert "namespace" in findings[0].message


def test_natural_language_title_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", title="Road Centerlines 2024")
    assert findings_for(validate(catalog.write()), "PTL-TTL-002") == []


def test_single_word_title_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", title="Roads")
    assert findings_for(validate(catalog.write()), "PTL-TTL-002") == []


def test_untitled_child_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    def drop_title(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "child":
                del link["title"]

    mutate_json(root / "catalog.json", drop_title)
    findings = findings_for(validate(root), "PTL-TTL-003")
    assert len(findings) == 1
    assert "no title" in findings[0].message


def test_untitled_item_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()

    def blank_title(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "item":
                link["title"] = ""

    mutate_json(root / "roads" / "collection.json", blank_title)
    assert len(findings_for(validate(root), "PTL-TTL-003")) == 1
