from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

from rashid.catalog import CatalogGraph
from tests.conftest import (
    CatalogBuilder,
    mutate_json,
    write_language_trees,
    write_organizing_catalog_layout,
)

pytestmark = pytest.mark.unit


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    return CatalogGraph.load(catalog.write())


def test_kind_detection(catalog: CatalogBuilder) -> None:
    roads = catalog.collection("roads")
    roads.item("roads-2024")
    graph = _graph(catalog)
    kinds = {str(path): node.kind for path, node in graph.nodes.items()}
    assert kinds == {
        "catalog.json": "catalog",
        "roads/collection.json": "collection",
        "roads/roads-2024/roads-2024.json": "item",
    }
    assert graph.root is not None and graph.root.id == "root"


def test_non_stac_json_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    styles = root / "roads" / "styles"
    styles.mkdir()
    (styles / "default.json").write_text(json.dumps({"version": 8, "layers": []}))
    graph = CatalogGraph.load(root)
    assert PurePosixPath("roads/styles/default.json") not in graph.nodes


def test_hidden_directories_are_skipped(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    hidden = root / ".cache"
    hidden.mkdir()
    (hidden / "collection.json").write_text(json.dumps({"type": "Collection", "id": "x"}))
    graph = CatalogGraph.load(root)
    assert PurePosixPath(".cache/collection.json") not in graph.nodes


def test_unparseable_structural_json_becomes_parse_error_node(
    catalog: CatalogBuilder,
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "collection.json").write_text("{not json")
    graph = CatalogGraph.load(root)
    node = graph.nodes[PurePosixPath("roads/collection.json")]
    assert node.kind == "unknown"
    assert node.parse_error is not None


def test_resolve_link_normalizes_dot_segments(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    graph = _graph(catalog)
    collection = graph.nodes[PurePosixPath("roads/collection.json")]
    assert graph.resolve_link(collection, "../catalog.json") is graph.root
    assert graph.resolve_link(collection, "./../catalog.json") is graph.root


def test_resolve_link_rejects_absolute_and_escaping_hrefs(catalog: CatalogBuilder) -> None:
    graph = _graph(catalog)
    root = graph.root
    assert root is not None
    assert graph.resolve_link(root, "https://example.org/catalog.json") is None
    assert graph.resolve_link(root, "/catalog.json") is None
    assert graph.resolve_link(root, "../outside.json") is None
    assert graph.resolve_link(root, "") is None


def test_containment(catalog: CatalogBuilder) -> None:
    roads = catalog.collection("roads")
    roads.item("roads-2024")
    env = catalog.subcatalog("environment")
    env.collection("air-quality")
    graph = _graph(catalog)
    root = graph.root
    assert root is not None
    collection = graph.nodes[PurePosixPath("roads/collection.json")]
    item = graph.nodes[PurePosixPath("roads/roads-2024/roads-2024.json")]
    subcatalog = graph.nodes[PurePosixPath("environment/catalog.json")]
    nested = graph.nodes[PurePosixPath("environment/air-quality/collection.json")]

    assert graph.parent_of(root) is None
    assert graph.parent_of(collection) is root
    assert graph.parent_of(item) is collection
    assert graph.parent_of(subcatalog) is root
    assert graph.parent_of(nested) is subcatalog
    assert {n.path for n in graph.children_of(root)} == {collection.path, subcatalog.path}
    assert graph.children_of(collection) == [item]


def test_enclosing_collection_walks_past_intermediate_catalogs(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(write_organizing_catalog_layout(catalog))
    root = graph.root
    assert root is not None
    collection = graph.nodes[PurePosixPath("roads/collection.json")]
    organizing = graph.nodes[PurePosixPath("roads/2024/catalog.json")]
    item = graph.nodes[PurePosixPath("roads/2024/roads-2024/roads-2024.json")]

    # the item's direct parent is the organizing catalog, but the collection
    # that encloses it is two levels up
    assert graph.parent_of(item) is organizing
    assert graph.enclosing_collection_of(item) is collection
    assert graph.enclosing_collection_of(organizing) is collection
    # nothing above these is a collection
    assert graph.enclosing_collection_of(collection) is None
    assert graph.enclosing_collection_of(root) is None


def test_items_of_reaches_items_under_an_organizing_catalog(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(write_organizing_catalog_layout(catalog))
    collection = graph.nodes[PurePosixPath("roads/collection.json")]
    organizing = graph.nodes[PurePosixPath("roads/2024/catalog.json")]
    item = graph.nodes[PurePosixPath("roads/2024/roads-2024/roads-2024.json")]

    # direct containment stops at the organizing catalog and never sees the item
    assert graph.children_of(collection) == [organizing]
    assert graph.items_of(collection) == [item]


def test_items_of_leaves_a_nested_collections_items_to_that_collection(
    catalog: CatalogBuilder,
) -> None:
    """An item belongs to the nearest collection above it, not the outermost."""
    outer = catalog.collection("roads")
    outer.item("roads-2024")
    graph = _graph(catalog)
    collection = graph.nodes[PurePosixPath("roads/collection.json")]
    item = graph.nodes[PurePosixPath("roads/roads-2024/roads-2024.json")]

    assert graph.items_of(collection) == [item]
    assert graph.items_of(item) == []


def test_dir_listing_and_file_exists(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    graph = _graph(catalog)
    assert graph.file_exists(PurePosixPath("AGENTS.md"))
    assert graph.file_exists(PurePosixPath("roads/README.md"))
    assert not graph.file_exists(PurePosixPath("roads/missing.md"))


def test_missing_root_directory(tmp_path: Path) -> None:
    graph = CatalogGraph.load(tmp_path)
    assert graph.root is None
    assert graph.nodes == {}


def test_translation_roots_follow_alternate_links(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(write_language_trees(catalog))
    assert [str(node.path) for node in graph.translation_roots()] == ["ro/catalog.json"]


def test_language_root_of_places_every_node_in_a_tree(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(write_language_trees(catalog))
    trees = {
        str(node.path): str(graph.language_root_of(node).path)  # type: ignore[union-attr]
        for node in graph.iter()
    }
    assert trees == {
        "catalog.json": "catalog.json",
        "roads/collection.json": "catalog.json",
        "ro/catalog.json": "ro/catalog.json",
        "ro/roads/collection.json": "ro/catalog.json",
    }


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("not-a-link", id="link-is-not-an-object"),
        pytest.param({"rel": "alternate", "type": "application/json"}, id="link-has-no-href"),
        pytest.param(
            {"rel": "alternate", "href": 7, "type": "application/json"}, id="href-is-not-a-string"
        ),
        pytest.param(
            {"rel": "alternate", "href": "./ro/catalog.json", "type": "text/html"},
            id="alternate-is-another-media-type",
        ),
        pytest.param(
            {"rel": "alternate", "href": "./roads/collection.json", "type": "application/json"},
            id="alternate-is-not-a-catalog",
        ),
        pytest.param(
            {
                "rel": "alternate",
                "href": "https://example.org/ro/catalog.json",
                "type": "application/json",
            },
            id="alternate-is-absolute",
        ),
    ],
)
def test_a_malformed_alternate_link_names_no_translation(
    catalog: CatalogBuilder, link: object
) -> None:
    """Discovery is deliberately narrow: anything it cannot read is not a tree."""
    catalog.collection("roads")
    root = catalog.write()
    (root / "ro").mkdir()
    mutate_json(root / "catalog.json", lambda data: data["links"].append(link))
    assert CatalogGraph.load(root).translation_roots() == []


def test_a_catalog_without_a_readable_root_names_no_translations(tmp_path: Path) -> None:
    """Discovery starts at the root catalog, so an absent root ends it."""
    root = tmp_path / "catalog"
    (root / "roads").mkdir(parents=True)
    (root / "roads" / "collection.json").write_text(
        json.dumps({"type": "Collection", "id": "roads"}), encoding="utf-8"
    )
    graph = CatalogGraph.load(root)
    assert graph.root is None
    assert graph.translation_roots() == []
