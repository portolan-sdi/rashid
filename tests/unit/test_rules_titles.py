from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from rashid import validate
from rashid.cli import main
from rashid.model import Severity
from tests.conftest import (
    CatalogBuilder,
    findings_for,
    mutate_json,
    write_organizing_catalog_layout,
)

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
    assert len(findings) == 2  # Collection title and the root's child-link title.
    assert all(f.severity is Severity.WARNING for f in findings)
    assert all("raw slug" in f.message for f in findings)


def test_namespaced_title_warns(catalog: CatalogBuilder) -> None:
    catalog.collection("addresses", title="os:Address")
    findings = findings_for(validate(catalog.write()), "PTL-TTL-002")
    assert len(findings) == 2
    assert all("namespace" in f.message for f in findings)


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


@pytest.mark.parametrize("bad_title", ["scene_slug_2024", "ns:Scene"])
@pytest.mark.parametrize("organizing", [False, True])
def test_title_positions_through_cli(
    catalog: CatalogBuilder, bad_title: str, organizing: bool
) -> None:
    if organizing:
        root = write_organizing_catalog_layout(catalog)
    else:
        catalog.collection("roads").item("roads-2024")
        root = catalog.write()
    positions = {
        "catalog.json": "/links/3/title",  # child collection
        "roads/collection.json": "/links/4/title",  # child or item link
    }
    if organizing:
        positions["roads/2024/catalog.json"] = "/links/2/title"
        positions["roads/2024/roads-2024/roads-2024.json"] = "/properties/title"
    else:
        positions["roads/roads-2024/roads-2024.json"] = "/properties/title"
    for file, pointer in positions.items():

        def set_title(data: dict, pointer: str = pointer) -> None:
            if pointer == "/properties/title":
                data["properties"]["title"] = bad_title
                data["title"] = "ignored_top_level_slug"
                data["assets"]["data"]["title"] = "ignored_asset_slug"
            else:
                data["links"][int(pointer.split("/")[2])]["title"] = bad_title
                # Missing own titles must not hide unreadable link titles.
                data.pop("title", None)
                data["links"].append(
                    {"rel": "about", "href": "https://example.org", "title": bad_title}
                )

        mutate_json(root / file, set_title)

    result = CliRunner().invoke(main, ["check", "--json", "--no-data", str(root)])
    assert result.exit_code == 1  # The absent catalog/collection titles remain errors.
    findings = [f for f in json.loads(result.output)["findings"] if f["rule_id"] == "PTL-TTL-002"]
    assert {(f["path"], f["json_pointer"]) for f in findings} == set(positions.items())
    assert len(findings) == len(positions)
    assert all(f["severity"] == "warning" for f in findings)

    # Check the same positions with readable titles and tolerate absent/malformed
    # optional Item titles without broadening the title-presence requirements.
    for file, pointer in positions.items():

        def clear_title(data: dict, pointer: str = pointer) -> None:
            if pointer == "/properties/title":
                data["properties"]["title"] = None
            else:
                data["links"][int(pointer.split("/")[2])]["title"] = "Roads in 2024"

        mutate_json(root / file, clear_title)
    result = CliRunner().invoke(main, ["check", "--json", "--no-data", str(root)])
    findings = json.loads(result.output)["findings"]
    assert not [f for f in findings if f["rule_id"] == "PTL-TTL-002"]
    assert not [
        f
        for f in findings
        if f["path"].endswith("roads-2024.json") and f["rule_id"] in ("PTL-TTL-001", "PTL-TTL-003")
    ]


@pytest.mark.parametrize("rel", ["child", "item"])
@pytest.mark.parametrize("title", ["road_centerlines", None])
def test_link_title_pointer_keeps_original_index_through_cli(
    catalog: CatalogBuilder, rel: str, title: str | None
) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda data: data.__setitem__(
            "links", [None, {"rel": rel, "href": "./missing.json", "title": title}]
        ),
    )
    result = CliRunner().invoke(main, ["check", "--json", "--no-data", str(root)])
    assert result.exit_code == 1  # The non-object link remains a structural error.
    rule_id = "PTL-TTL-002" if title else "PTL-TTL-003"
    findings = [f for f in json.loads(result.output)["findings"] if f["rule_id"] == rule_id]
    assert len(findings) == 1
    assert findings[0]["json_pointer"] == "/links/1/title"
    assert findings[0]["path"] == "catalog.json"
