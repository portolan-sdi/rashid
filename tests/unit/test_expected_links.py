"""Structural links generated for fixers agree with metadata validation."""

from __future__ import annotations

import json
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import pytest

from rashid_core import ValidationOptions, validate
from rashid_core.api import STRUCTURAL_RELS, CatalogGraph, expected_links
from rashid_core.rules.links import RequiredLinksRule, StructuralLinkTypeRule
from tests.conftest import (
    CatalogBuilder,
    mutate_json,
    write_language_trees,
    write_organizing_catalog_layout,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("tree", ["reference", "languages", "encoded"])
def test_rebuild_structural_links(catalog: CatalogBuilder, tmp_path: Path, tree: str) -> None:
    if tree == "reference":
        source = Path(__file__).parents[1] / "fixtures" / "reference-catalog"
        root = shutil.copytree(source, tmp_path / "reference")
    elif tree == "languages":
        root = write_language_trees(catalog)
    else:
        for directory in ("my roads", "my %20 roads", "東京", "a:b"):
            catalog.collection(directory).item("été 2024")
        root = catalog.write()
        # Start with valid URI references so the baseline cannot hide encoding failures.
        for path in root.rglob("*.json"):
            mutate_json(
                path,
                lambda data: [
                    link.update(href=quote(link["href"], safe="/")) for link in data["links"]
                ],
            )

    options = ValidationOptions(structural=False, extensions=False, data=False)

    def findings() -> list[str]:
        return [
            finding.rule_id
            for finding in validate(root, options=options).findings
            if finding.rule_id.startswith(("PTL-LNK", "PTL-FIL"))
        ]

    assert not findings()
    graph = CatalogGraph.load(root)
    generated = {node.path: expected_links(graph, node) for node in graph.iter()}
    for node in graph.iter():
        node.data["links"] = [
            link for link in node.data["links"] if link.get("rel") not in STRUCTURAL_RELS
        ]
        node.abs_path.write_text(json.dumps(node.data), encoding="utf-8")
    # Reload after removing links: alternate links alone must identify the language trees.
    graph = CatalogGraph.load(root)
    for node in graph.iter():
        links = expected_links(graph, node)
        assert [(link.rel, link.href, link.type) for link in links] == [
            (link.rel, link.href, link.type) for link in generated[node.path]
        ]
        for link in links:
            assert graph.resolve_structural_link(node, link.href) == (True, link.target)
            assert graph.language_root_of(link.target) is graph.language_root_of(node)
            node.data["links"].append({"rel": link.rel, "href": link.href, "type": link.type})
        node.abs_path.write_text(json.dumps(node.data), encoding="utf-8")
    assert not findings()


def test_expected_ancestors_and_media_types(catalog: CatalogBuilder) -> None:
    root = write_organizing_catalog_layout(catalog, "../../collection.json")
    graph = CatalogGraph.load(root)
    item = next(graph.iter("item"))
    links = expected_links(graph, item)
    assert [(link.rel, str(link.target.path), link.href, link.type) for link in links] == [
        ("root", "catalog.json", "../../../catalog.json", "application/json"),
        ("parent", "roads/2024/catalog.json", "../catalog.json", "application/json"),
        ("collection", "roads/collection.json", "../../collection.json", "application/json"),
    ]
    with pytest.raises(FrozenInstanceError):
        links[0].rel = "parent"


def test_missing_targets_do_not_hide_required_links(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("road-2024")
    root = catalog.write()
    (root / "catalog.json").unlink()
    (root / "roads" / "collection.json").unlink()
    graph = CatalogGraph.load(root)
    item = next(graph.iter("item"))
    item.data["links"] = []
    assert expected_links(graph, item) == ()
    assert [finding.message for finding in RequiredLinksRule().check(item, graph)] == [
        f"missing required structural link rel:'{rel}'" for rel in ("root", "parent", "collection")
    ]
    item.data["links"] = [{"rel": "item", "href": "./road-2024.json"}]
    assert [finding.message for finding in StructuralLinkTypeRule().check(item, graph)] == [
        "link rel:'item' has type None, expected 'application/geo+json'"
    ]


def test_mirror_paths_are_not_encoded_twice(catalog: CatalogBuilder) -> None:
    catalog.collection("my%20roads").item("%C3%A9t%C3%A9")
    graph = CatalogGraph.load(catalog.write())
    graph.base_url = "https://example.org/"
    assert graph.root is not None
    child = next(link for link in expected_links(graph, graph.root) if link.rel == "child")
    assert child.href == "./my%20roads/collection.json"
    assert graph.resolve_link(graph.root, child.href) is child.target
    child.target.kind = "unknown"
    assert expected_links(graph, child.target) == ()
    assert child.target.path == PurePosixPath("my%20roads/collection.json")


@pytest.mark.parametrize("contents", ['{"note": "not STAC"}', "invalid JSON"])
def test_rebuild_ignores_unknown_children(catalog: CatalogBuilder, contents: str) -> None:
    root = catalog.write()
    misc = root / "misc" / "catalog.json"
    misc.parent.mkdir()
    misc.write_text(contents, encoding="utf-8")
    graph = CatalogGraph.load(root)
    assert graph.root is not None
    node = graph.root
    node.data["links"] = [
        {"rel": link.rel, "href": link.href, "type": link.type}
        for link in expected_links(graph, node)
    ]
    node.abs_path.write_text(json.dumps(node.data), encoding="utf-8")
    findings = validate(root).findings
    assert not [
        finding for finding in findings if finding.rule_id in ("PTL-LNK-002", "PTL-LNK-006")
    ]
