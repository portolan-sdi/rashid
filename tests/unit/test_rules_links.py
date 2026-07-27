from __future__ import annotations

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, findings_for, mutate_json, rule_ids

pytestmark = pytest.mark.unit


def test_collection_missing_parent_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "parent"]),
    )
    report = validate(root)
    findings = findings_for(report, "PTL-LNK-001")
    assert len(findings) == 1
    assert "parent" in findings[0].message
    assert findings[0].path == "roads/collection.json"


def test_root_catalog_needs_no_parent_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-LNK-001") == []


def test_item_missing_collection_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "roads-2024" / "roads-2024.json",
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link["rel"] != "collection"]
        ),
    )
    findings = findings_for(validate(root), "PTL-LNK-001")
    assert len(findings) == 1
    assert "collection" in findings[0].message


def test_contained_object_without_child_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "child"]),
    )
    findings = findings_for(validate(root), "PTL-LNK-002")
    assert len(findings) == 1
    assert "roads/collection.json" in findings[0].message
    assert findings[0].path == "catalog.json"


def test_on_disk_item_without_item_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "item"]),
    )
    findings = findings_for(validate(root), "PTL-LNK-002")
    assert len(findings) == 1
    assert "item link" in findings[0].message


def test_child_link_with_wrong_type(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    def set_type(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "child":
                link["type"] = "application/geo+json"

    mutate_json(root / "catalog.json", set_type)
    findings = findings_for(validate(root), "PTL-LNK-003")
    assert len(findings) == 1
    assert "expected 'application/json'" in findings[0].message


def test_item_link_with_wrong_type(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()

    def set_type(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "item":
                link["type"] = "application/json"

    mutate_json(root / "roads" / "collection.json", set_type)
    findings = findings_for(validate(root), "PTL-LNK-003")
    assert len(findings) == 1
    assert "expected 'application/geo+json'" in findings[0].message


def test_absolute_structural_href(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    def absolutize(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "child":
                link["href"] = "https://example.org/roads/collection.json"

    mutate_json(root / "catalog.json", absolutize)
    report = validate(root)
    findings = findings_for(report, "PTL-LNK-004")
    assert len(findings) == 1
    assert "must be relative" in findings[0].message
    # LNK-006 does not double-report absolute hrefs
    assert all("child" not in f.message for f in findings_for(report, "PTL-LNK-006"))


def test_self_link_is_rejected(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {"rel": "self", "href": "./catalog.json", "type": "application/json"}
        ),
    )
    assert len(findings_for(validate(root), "PTL-LNK-005")) == 1


def test_dangling_child_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    def dangle(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "child":
                link["href"] = "./missing/collection.json"

    mutate_json(root / "catalog.json", dangle)
    report = validate(root)
    findings = findings_for(report, "PTL-LNK-006")
    assert len(findings) == 1
    assert "does not resolve to any file" in findings[0].message
    # the on-disk collection is now unlinked too
    assert len(findings_for(report, "PTL-LNK-002")) == 1


def test_child_link_to_non_stac_file(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    def retarget(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "child":
                link["href"] = "./README.md"

    mutate_json(root / "catalog.json", retarget)
    findings = findings_for(validate(root), "PTL-LNK-006")
    assert len(findings) == 1
    assert "not a recognizable STAC object" in findings[0].message


def test_root_link_to_wrong_object(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()

    def retarget(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "root":
                link["href"] = "../collection.json"

    mutate_json(root / "roads" / "roads-2024" / "roads-2024.json", retarget)
    findings = findings_for(validate(root), "PTL-LNK-006")
    assert len(findings) == 1
    assert "root catalog" in findings[0].message


def test_child_link_to_item_is_wrong_kind(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    root = catalog.write()

    def retarget(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "item":
                link["rel"] = "child"

    mutate_json(root / "roads" / "collection.json", retarget)
    findings = findings_for(validate(root), "PTL-LNK-006")
    assert len(findings) == 1
    assert "catalog or collection" in findings[0].message


def test_escape_of_catalog_root_is_dangling(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def escape(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "root":
                link["href"] = "../../elsewhere/catalog.json"

    mutate_json(root / "catalog.json", escape)
    findings = findings_for(validate(root), "PTL-LNK-006")
    assert len(findings) == 1
    assert "does not resolve" in findings[0].message


def test_clean_catalog_has_no_link_findings(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    report = validate(catalog.write())
    assert not rule_ids(report) & {
        "PTL-LNK-001",
        "PTL-LNK-002",
        "PTL-LNK-003",
        "PTL-LNK-004",
        "PTL-LNK-005",
        "PTL-LNK-006",
    }


def test_structural_link_with_no_type_field(catalog: CatalogBuilder) -> None:
    # regression guard for the golden-catalog audit's claim 12: a structural
    # link that omits "type" entirely must be flagged, not silently passed
    catalog.collection("roads")
    root = catalog.write()

    def drop_type(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "child":
                del link["type"]

    mutate_json(root / "catalog.json", drop_type)
    findings = findings_for(validate(root), "PTL-LNK-003")
    assert len(findings) == 1
    assert "has type None" in findings[0].message
