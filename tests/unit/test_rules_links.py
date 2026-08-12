from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    CatalogBuilder,
    findings_for,
    mutate_json,
    rule_ids,
    write_organizing_catalog_layout,
)

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


def test_collection_link_across_organizing_catalog(catalog: CatalogBuilder) -> None:
    # issue #61: a catalog may sit below a collection to organize its items,
    # so the item's parent is that catalog while its collection link points
    # two levels up at the enclosing collection. Both are correct.
    root = write_organizing_catalog_layout(catalog, "../../collection.json")
    assert findings_for(validate(root), "PTL-LNK-006") == []


def test_collection_link_equal_to_direct_parent(catalog: CatalogBuilder) -> None:
    # the flat layout, where the enclosing collection is also the direct parent
    catalog.collection("roads").item("roads-2024")
    assert findings_for(validate(catalog.write()), "PTL-LNK-006") == []


def test_collection_link_to_non_collection(catalog: CatalogBuilder) -> None:
    # pointing at the organizing catalog rather than the collection
    root = write_organizing_catalog_layout(catalog, "../catalog.json")
    findings = findings_for(validate(root), "PTL-LNK-006")
    assert len(findings) == 1
    assert "enclosing collection" in findings[0].message
    assert findings[0].path == "roads/2024/roads-2024/roads-2024.json"


def test_collection_link_to_unrelated_collection(catalog: CatalogBuilder) -> None:
    # a real collection that does not enclose the item is still the wrong target
    catalog.collection("rivers")
    root = write_organizing_catalog_layout(catalog, "../../../rivers/collection.json")
    findings = findings_for(validate(root), "PTL-LNK-006")
    assert len(findings) == 1
    assert "roads/collection.json" in findings[0].message


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


# --------------------------------------------------------------- catalog logo


def _add_icon(root, **fields):
    """Put a rel:'icon' link with the given fields on the root catalog."""
    mutate_json(root / "catalog.json", lambda d: d["links"].append({"rel": "icon", **fields}))


def test_icon_link_with_displayable_type_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="./_assets/logo.png", type="image/png", title="Demo Org")
    report = validate(root)
    assert findings_for(report, "PTL-LNK-007") == []
    assert findings_for(report, "PTL-LNK-008") == []
    assert findings_for(report, "PTL-LNK-009") == []


def test_icon_link_without_type(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="./_assets/logo.png", title="Demo Org")
    findings = findings_for(validate(root), "PTL-LNK-007")
    assert len(findings) == 1
    assert "declares no type" in findings[0].message
    assert findings[0].path == "catalog.json"


def test_icon_link_with_undisplayable_type(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="./_assets/logo.pdf", type="application/pdf", title="Demo Org")
    findings = findings_for(validate(root), "PTL-LNK-007")
    assert len(findings) == 1
    assert "no browser renders" in findings[0].message


def test_icon_link_accepts_svg(catalog: CatalogBuilder) -> None:
    # stac-js drops SVG, but the spec lists it, so the validator must not.
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="./_assets/logo.svg", type="image/svg+xml", title="Demo Org")
    assert findings_for(validate(root), "PTL-LNK-007") == []


def test_icon_link_without_title_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="./_assets/logo.png", type="image/png")
    findings = findings_for(validate(root), "PTL-LNK-008")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING


def test_icon_link_with_blank_title_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="./_assets/logo.png", type="image/png", title="   ")
    assert len(findings_for(validate(root), "PTL-LNK-008")) == 1


def test_icon_link_with_absolute_href_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, href="https://example.org/logo.png", type="image/png", title="Demo Org")
    findings = findings_for(validate(root), "PTL-LNK-009")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "should be relative" in findings[0].message


def test_icon_link_without_href_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _add_icon(root, type="image/png", title="Demo Org")
    findings = findings_for(validate(root), "PTL-LNK-009")
    assert len(findings) == 1
    assert "has no href" in findings[0].message


def test_catalog_without_icon_link_is_clean(catalog: CatalogBuilder) -> None:
    # The logo is optional; its absence is never a finding.
    catalog.collection("roads")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-LNK-007") == []
    assert findings_for(report, "PTL-LNK-008") == []
    assert findings_for(report, "PTL-LNK-009") == []


def test_icon_link_on_a_collection_is_checked(catalog: CatalogBuilder) -> None:
    # core.md scopes publishing to the root, but STAC Browser renders per-entity
    # icons, so an icon found elsewhere is held to the same media-type rule.
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "icon", "href": "./logo.pdf", "type": "application/pdf", "title": "Roads"}
        ),
    )
    findings = findings_for(validate(root), "PTL-LNK-007")
    assert len(findings) == 1
    assert findings[0].path == "roads/collection.json"
