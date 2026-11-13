"""Link findings retain positions in malformed links arrays."""

from __future__ import annotations

from typing import Any

import pytest

from tests._https import HttpsServer
from tests.conftest import CatalogBuilder, mutate_json, write_language_trees
from tests.integration.test_href_behavior import _check

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "link,rule,pointers",
    [
        ({"rel": "via", "href": "./bad name.bin"}, "PTL-LNK-006", ["/links/1/href"]),
        ({"rel": "root", "href": "../catalog.json"}, "PTL-LNK-003", ["/links/1/type"]),
        (
            {"rel": "root", "href": "./absent.json", "type": "application/json"},
            "PTL-LNK-006",
            ["/links/1/href"],
        ),
        ({"rel": "icon", "href": "https://example.org/logo.png"}, "PTL-LNK-007", ["/links/1"]),
        ({"rel": "icon", "href": "https://example.org/logo.png"}, "PTL-LNK-008", ["/links/1"]),
        (
            {"rel": "icon", "href": "https://example.org/logo.png"},
            "PTL-LNK-009",
            ["/links/1/href"],
        ),
        (
            {"rel": "pmtiles", "href": "./data.pmtiles", "type": "application/json"},
            "PTL-VIZ-003",
            ["/links/1", "/links/1/type", "/stac_extensions"],
        ),
        (
            {"rel": "agents", "href": "./AGENTS.md", "type": "text/html"},
            "PTL-FIL-002",
            ["/links/1/type"],
        ),
        (
            {"rel": "describedby", "href": "./README.md", "type": "text/html"},
            "PTL-FIL-003",
            ["/links/1/type"],
        ),
    ],
)
def test_metadata_link_pointer_preserves_unfiltered_index(
    catalog: CatalogBuilder, link: dict[str, Any], rule: str, pointers: list[str]
) -> None:
    catalog.collection("roads", links=[None, link])
    report = _check(catalog.write(), "--no-data", "--no-live")
    findings = [
        f
        for f in report["findings"]
        if f["rule_id"] == rule and f["path"] == "roads/collection.json"
    ]
    assert sorted(f["json_pointer"] for f in findings) == pointers


def test_translation_link_pointer_preserves_unfiltered_index(catalog: CatalogBuilder) -> None:
    root = write_language_trees(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].__setitem__(
            slice(0, 0),
            [None, {"rel": "child", "href": "./ro/catalog.json", "type": "application/json"}],
        ),
    )
    findings = _check(root, "--no-data", "--no-live")["findings"]
    (finding,) = [f for f in findings if f["rule_id"] == "PTL-LNK-010"]
    assert finding["json_pointer"] == "/links/1"


@pytest.mark.parametrize("rel,rule", [("via", "PTL-LIV-006"), ("self", "PTL-LIV-007")])
def test_live_link_pointer_preserves_unfiltered_index(
    catalog: CatalogBuilder, https_server: HttpsServer, rel: str, rule: str
) -> None:
    catalog.overrides["links"] = [
        None,
        {"rel": rel, "href": https_server.url("wrong/catalog.json"), "type": "application/json"},
    ]
    findings = _check(catalog.write(), "--no-data", "--live", "--live-base-url", https_server.base)[
        "findings"
    ]
    (finding,) = [f for f in findings if f["rule_id"] == rule]
    assert finding["json_pointer"] == "/links/1/href"
