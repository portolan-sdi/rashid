from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit


def test_missing_agents_md(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    (root / "AGENTS.md").unlink()
    findings = findings_for(validate(root), "PTL-FIL-001")
    assert len(findings) == 1
    assert "AGENTS.md" in findings[0].message


def test_missing_readme_in_nested_collection(catalog: CatalogBuilder) -> None:
    env = catalog.subcatalog("environment")
    env.collection("air-quality")
    root = catalog.write()
    (root / "environment" / "air-quality" / "README.md").unlink()
    findings = findings_for(validate(root), "PTL-FIL-001")
    assert len(findings) == 1
    assert findings[0].path == "environment/air-quality/collection.json"
    assert "README.md" in findings[0].message


def test_wrong_case_filename_is_reported(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    (root / "README.md").rename(root / "readme.md")
    findings = findings_for(validate(root), "PTL-FIL-001")
    assert len(findings) == 1
    assert "case-sensitive" in findings[0].message
    assert "readme.md" in findings[0].message


def test_missing_agents_link(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != "agents"]),
    )
    findings = findings_for(validate(root), "PTL-FIL-002")
    assert len(findings) == 1
    assert "missing rel:'agents'" in findings[0].message


def test_agents_link_with_wrong_type(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def set_type(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "agents":
                link["type"] = "text/plain"

    mutate_json(root / "catalog.json", set_type)
    findings = findings_for(validate(root), "PTL-FIL-002")
    assert len(findings) == 1
    assert "text/markdown" in findings[0].message


def test_agents_link_to_wrong_file(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def retarget(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "agents":
                link["href"] = "./README.md"

    mutate_json(root / "catalog.json", retarget)
    findings = findings_for(validate(root), "PTL-FIL-002")
    assert len(findings) == 1
    assert "does not resolve to the sibling AGENTS.md" in findings[0].message


def test_absolute_agents_href(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def absolutize(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "agents":
                link["href"] = "https://example.org/AGENTS.md"

    mutate_json(root / "catalog.json", absolutize)
    findings = findings_for(validate(root), "PTL-FIL-002")
    assert len(findings) == 1
    assert "relative path" in findings[0].message


def test_missing_readme_link(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link["rel"] != "describedby"]
        ),
    )
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 1
    assert "missing rel:'describedby'" in findings[0].message
    assert "README.md" in findings[0].message


def test_readme_link_with_wrong_type(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def set_type(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "describedby":
                link["type"] = "text/plain"

    mutate_json(root / "catalog.json", set_type)
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 1
    assert "text/markdown" in findings[0].message


def test_readme_link_to_wrong_file(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def retarget(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "describedby":
                link["href"] = "./AGENTS.md"

    mutate_json(root / "catalog.json", retarget)
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 1
    assert "does not resolve to the sibling README.md" in findings[0].message


def test_absolute_readme_href(catalog: CatalogBuilder) -> None:
    root = catalog.write()

    def absolutize(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "describedby":
                link["href"] = "https://example.org/README.md"

    mutate_json(root / "catalog.json", absolutize)
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 1
    assert "relative path" in findings[0].message


def _append_link(path, link: dict) -> None:  # type: ignore[no-untyped-def]
    mutate_json(path, lambda d: d["links"].append(link))


def test_foreign_describedby_link_alongside_readme_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "data-dictionary.pdf").write_bytes(b"%PDF-1.4\n")
    _append_link(
        root / "roads" / "collection.json",
        {"rel": "describedby", "href": "./data-dictionary.pdf", "type": "application/pdf"},
    )
    assert findings_for(validate(root), "PTL-FIL-003") == []


def test_foreign_agents_link_alongside_agents_md_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "playbook.pdf").write_bytes(b"%PDF-1.4\n")
    _append_link(
        root / "roads" / "collection.json",
        {"rel": "agents", "href": "./playbook.pdf", "type": "application/pdf"},
    )
    assert findings_for(validate(root), "PTL-FIL-002") == []


def test_only_foreign_describedby_link_still_fails(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "data-dictionary.pdf").write_bytes(b"%PDF-1.4\n")

    def replace(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "describedby":
                link["href"] = "./data-dictionary.pdf"
                link["type"] = "application/pdf"

    mutate_json(root / "roads" / "collection.json", replace)
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 2
    assert {finding.json_pointer for finding in findings} == {"/links/3/type", "/links/3/href"}


def test_only_foreign_agents_link_still_fails(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "playbook.pdf").write_bytes(b"%PDF-1.4\n")

    def replace(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "agents":
                link["href"] = "./playbook.pdf"
                link["type"] = "application/pdf"

    mutate_json(root / "roads" / "collection.json", replace)
    findings = findings_for(validate(root), "PTL-FIL-002")
    assert len(findings) == 2
    assert {finding.json_pointer for finding in findings} == {"/links/2/type", "/links/2/href"}


def test_broken_readme_link_is_blamed_over_a_foreign_link(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "data-dictionary.pdf").write_bytes(b"%PDF-1.4\n")

    def set_type(d: dict) -> None:
        for link in d["links"]:
            if link["rel"] == "describedby":
                link["type"] = "text/plain"

    mutate_json(root / "roads" / "collection.json", set_type)
    _append_link(
        root / "roads" / "collection.json",
        {"rel": "describedby", "href": "./data-dictionary.pdf", "type": "application/pdf"},
    )
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/links/3/type"
    assert "text/markdown" in findings[0].message


def test_readme_link_on_collection(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link["rel"] != "describedby"]
        ),
    )
    findings = findings_for(validate(root), "PTL-FIL-003")
    assert len(findings) == 1
    assert findings[0].path == "roads/collection.json"


def _readme(root, *parts):  # type: ignore[no-untyped-def]
    path = root
    for part in parts:
        path = path / part
    return path / "README.md"


def test_default_readme_content_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    report = validate(catalog.write())
    assert findings_for(report, "PTL-FIL-004") == []
    assert findings_for(report, "PTL-FIL-005") == []


def test_empty_readme_is_an_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _readme(root, "roads").write_text("   \n", encoding="utf-8")
    findings = findings_for(validate(root), "PTL-FIL-004")
    assert len(findings) == 1
    assert "empty" in findings[0].message
    assert findings[0].path == "roads/collection.json"


def test_readme_without_heading_is_an_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _readme(root, "roads").write_text(
        "Road centerlines. License: CC-BY-4.0. Source: city GIS.\n", encoding="utf-8"
    )
    findings = findings_for(validate(root), "PTL-FIL-004")
    assert len(findings) == 1
    assert "heading" in findings[0].message


def test_missing_readme_is_not_a_content_finding(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _readme(root, "roads").unlink()
    report = validate(root)
    assert findings_for(report, "PTL-FIL-004") == []
    assert findings_for(report, "PTL-FIL-005") == []
    assert len(findings_for(report, "PTL-FIL-001")) == 1


def test_readme_without_license_mention_is_a_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _readme(root, "roads").write_text(
        "# Roads\n\nCenterlines produced by the city GIS office.\n", encoding="utf-8"
    )
    findings = findings_for(validate(root), "PTL-FIL-005")
    assert len(findings) == 1
    assert "license" in findings[0].message
    assert findings[0].severity is Severity.WARNING


def test_readme_without_provenance_mention_is_a_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    _readme(root, "roads").write_text(
        "# Roads\n\nCenterlines for the metro area. License: CC-BY-4.0.\n", encoding="utf-8"
    )
    findings = findings_for(validate(root), "PTL-FIL-005")
    assert len(findings) == 1
    assert "provenance" in findings[0].message


def test_catalog_readme_sections_are_not_graded(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    # An organizing catalog's README describes structure, not licensing.
    (root / "README.md").write_text("# Demo Catalog\n\nThematic groupings.\n", encoding="utf-8")
    report = validate(root)
    assert findings_for(report, "PTL-FIL-004") == []
    assert findings_for(report, "PTL-FIL-005") == []
