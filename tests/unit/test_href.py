"""Pure policy contracts for the href owner; behavioral cases use the CLI."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from rashid._href import (
    crawl_prefix,
    is_absolute_href,
    is_catalog_url,
    is_https_href,
    is_schema_url,
    live_host_href,
    location_key,
    normalize_live_base,
    object_location,
    relative_path,
    resolve_live_base,
    safe_crawl_path,
    self_link_base,
    split_catalog_url,
    structural_prefix,
)
from rashid.catalog import CatalogGraph
from tests.conftest import CatalogBuilder, nest_items_under_organizing_catalog

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "href,absolute,https,catalog,schema",
    [
        ("./file.json", False, False, False, False),
        ("/file.json", True, False, False, False),
        ("//host/file.json", True, False, False, False),
        ("s3://bucket/file", True, False, False, False),
        ("https://host/file", True, True, True, True),
        ("HTTPS://host/file", True, True, True, False),
        ("https:///file", True, True, False, True),
    ],
)
def test_absolute_and_fetch_policies(
    href: str, absolute: bool, https: bool, catalog: bool, schema: bool
) -> None:
    assert is_absolute_href(href) is absolute
    assert is_https_href(href) is https
    assert is_catalog_url(href) is catalog
    assert is_schema_url(href) is schema


@pytest.mark.parametrize(
    "href,structural,crawled,live",
    [
        ("../catalog.json", "../catalog.json", "catalog.json", "https://h/cat/catalog.json"),
        ("https://h/cat/a", "a", "a", "https://h/cat/a"),
        ("https://h/else/a", None, None, "https://h/else/a"),
        ("https://else/cat/a", None, None, None),
        ("http://h/cat/a", None, None, None),
        ("/a", None, None, None),
        ("../../outside.json", "../../outside.json", None, None),
    ],
)
def test_three_containment_policies(
    href: str, structural: str | None, crawled: str | None, live: str | None
) -> None:
    base = "https://h/cat/"
    assert structural_prefix(href, base) == structural
    assert crawl_prefix(base, "roads", href) == crawled
    assert live_host_href("roads", href, base) == live


def test_structural_prefix_allows_non_https_self_bases() -> None:
    assert structural_prefix("http://h/cat/a", "http://h/cat/") == "a"
    assert crawl_prefix("http://h/cat/", "", "http://h/cat/a") is None
    assert structural_prefix("https://h/cat/a", None) is None
    assert live_host_href("roads", "./a", None) is None
    assert live_host_href("roads", "https://h/a", None) == "https://h/a"
    assert live_host_href("roads", "https://H/a", "https://h/cat/") == "https://H/a"


@pytest.mark.parametrize("candidate", ["", ".", "..", "../a", "/a", "a\\b", "a\x00b"])
def test_crawl_filesystem_safety(candidate: str) -> None:
    assert safe_crawl_path(candidate) is None


def test_graph_and_crawl_keep_their_path_policies() -> None:
    # Graph rejects a root-level dots prefix; the crawl rejects separators
    # that could escape the temporary tree on Windows. Preserve both policies.
    assert relative_path(".", "..hidden/a") is None
    assert safe_crawl_path("..hidden/a") == "..hidden/a"
    assert relative_path("roads", "../catalog.json") == "catalog.json"
    assert relative_path("roads", "https://h/a") is None
    assert relative_path(".", "") is None
    assert relative_path(".", "./") is None
    assert safe_crawl_path("a/../b") == "b"


@pytest.mark.parametrize(
    "explicit,crawled,claimed,expected",
    [
        (None, None, None, (None, False)),
        ("https://explicit", "https://crawl/", "https://self/", ("https://explicit/", True)),
        (None, "https://crawl///", "https://self/", ("https://crawl/", True)),
        (None, None, "https://self/", ("https://self/", False)),
        (None, None, "http://self/", (None, False)),
        (None, None, "https:///missing-host", (None, False)),
    ],
)
def test_live_base_precedence(
    explicit: str | None,
    crawled: str | None,
    claimed: str | None,
    expected: tuple[str | None, bool],
) -> None:
    assert resolve_live_base(explicit, crawled, claimed) == expected


def test_live_base_validation_and_location_normalization() -> None:
    assert normalize_live_base("HTTPS://H/cat///") == "HTTPS://H/cat/"
    with pytest.raises(ValueError, match="live base_url must be an https URL"):
        resolve_live_base("http://explicit/", "https://crawl/", "https://self/")
    with pytest.raises(ValueError, match="live base_url must be an https URL"):
        normalize_live_base("https:///missing-host")
    assert location_key("https://H:443/a/../b//") == location_key("https://h/b/")
    assert location_key("https://h:8443/b") != location_key("https://h/b")
    assert split_catalog_url("HTTPS://H/cat/catalog.json?q#f") == (
        "https://h/cat/",
        "https://h/cat/catalog.json",
    )


def test_self_base_requires_the_root_filename(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    graph = CatalogGraph.load(root)
    node = graph.root
    assert node is not None
    assert self_link_base(None) is None
    for links in (
        None,
        [None],
        [{"rel": "root"}],
        [{"rel": "self", "href": "./catalog.json"}],
        [{"rel": "self", "href": "https://h/other.json"}],
    ):
        node.data["links"] = links
        assert self_link_base(node) is None
    node.data["links"] = [{"rel": "self", "href": "https://h/cat/catalog.json"}]
    assert self_link_base(node) == "https://h/cat/"


def test_rebuilding_graph_indexes_from_source_is_order_independent(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    for name in ("a", "b", "c"):
        collection.item(name)
    root = catalog.write()
    nest_items_under_organizing_catalog(root, root / "roads")
    loaded = CatalogGraph.load(root)
    paths = list(loaded.nodes)
    expected = [PurePosixPath(f"roads/2024/{name}/{name}.json") for name in ("a", "b", "c")]
    # Exhaust all rotations and their reversals; every cache is rebuilt from
    # the original nodes rather than patched after building another index.
    for offset in range(len(paths)):
        order = paths[offset:] + paths[:offset]
        for permutation in (order, order[::-1]):
            rebuilt = CatalogGraph(root, nodes={path: loaded.nodes[path] for path in permutation})
            owner = rebuilt.nodes[PurePosixPath("roads/collection.json")]
            assert [n.path for n in rebuilt.items_of(owner)] == expected
            before = [n.path for n in rebuilt.children_of(owner)]
            assert [n.path for n in rebuilt.items_of(owner)] == expected
            assert [n.path for n in rebuilt.children_of(owner)] == before
            assert all(rebuilt.enclosing_collection_of(n) is owner for n in rebuilt.items_of(owner))
            # The href owner must give an idempotent path when rebuilding links.
            for path in expected:
                assert relative_path(".", str(path)) == str(path)


@pytest.mark.parametrize("suffix", ["?token=x#piece", "#piece", "?token=%3F%23", "?#", "?"])
def test_suffix_is_ignored_for_containment_and_retained_for_wire_and_live(suffix: str) -> None:
    from rashid._href import containment_href, href_suffix
    from rashid.remote import wire_url

    href = "./a%20b.parquet" + suffix
    assert containment_href(href) == "./a%20b.parquet"
    assert href_suffix(href) == suffix
    assert relative_path("roads", href) == "roads/a%20b.parquet"
    assert crawl_prefix("https://h/cat/", "roads", href) == "roads/a%20b.parquet" + suffix
    assert live_host_href("roads", href, "https://h/cat/") == (
        "https://h/cat/roads/a%20b.parquet" + suffix
    )
    assert wire_url("https://h/cat/", "roads/a%20b.parquet" + suffix) == (
        "https://h/cat/roads/a%20b.parquet" + suffix
    )
    assert wire_url("https://h/cat/", "a b.parquet" + suffix) == (
        "https://h/cat/a%20b.parquet" + suffix
    )


def test_uri_reference_grammar_preserves_encoded_and_reserved_characters() -> None:
    from rashid._href import is_uri_reference

    for href in (
        "",
        "?",
        "#",
        "./a%20b.parquet",
        "../file;v=1",
        "https://h/a?x=%23%3F#fragment",
        "https://user:password@h:443/a",
        "https://[::1]/a",
        "mailto:a@example.org",
        "urn:example:animal:ferret:nose",
        "//host/path",
        "/absolute/path",
        "./x:y",
    ):
        assert is_uri_reference(href), href
    for href in (
        "./a b",
        "./a%",
        "./a%2x",
        "./a\\b",
        "./café",
        "./a[0]",
        "./a?q=[x]",
        "./a#one#two",
        "1scheme:path",
        ":path",
        "https://[broken",
        "https://h:port/a",
        "\nhttps://h/a",
        "https://h/a\r",
        "https://h/a\x7f",
        "https://h/a\x00",
    ):
        assert not is_uri_reference(href), href


@pytest.mark.parametrize(
    "left,right,equal",
    [
        ("https://HOST:443/a", "https://host/a#fragment", True),
        ("http://host:80/a", "http://host/a", True),
        ("https://[::1]:443/a", "https://[::1]/a", True),
        ("https://host:444/a", "https://host/a", False),
        ("https://host:bad/a", "https://host/a", False),
        ("https://host/%61", "https://host/a", True),
        ("https://host/a%2fb", "https://host/a%2Fb", False),
        ("https://host/a%3Bb", "https://host/a;b", True),
        ("https://host/a%20b", "https://host/a b", True),
        ("https://host/%E6%9D%B1%E4%BA%AC", "https://host/東京", True),
        ("https://host/a%3Bb?x=1", "https://host/a;b?x=1", False),
        ("https://host/%61?x=1", "https://host/a?x=1", False),
        ("https://host/a%2Fb", "https://host/a/b", False),
        ("https://host/a%3Fb", "https://host/a?b", False),
        ("https://host/a%23b", "https://host/a#b", False),
        ("https://host/a//b", "https://host/a/b", False),
        ("https://host/a/../b", "https://host/b", False),
        ("https://host/a/%2e/b", "https://host/a/./b", False),
        ("https://host/a?x=1", "https://host/a?x=2", False),
    ],
)
def test_object_location(left: str, right: str, equal: bool) -> None:
    assert (object_location(left) == object_location(right)) is equal


def test_file_location(tmp_path: Path) -> None:
    path = tmp_path / "a b.json"
    assert object_location(path.as_uri()) == object_location(
        str(tmp_path.as_uri()) + "/x/../a%20b.json"
    )


@pytest.mark.parametrize("href", ["https://[broken/a", "https://host／a"])
def test_malformed_authorities_are_safe_at_every_href_entry_point(href: str) -> None:
    from rashid._href import is_uri_reference, parse_href, split_href

    assert parse_href(href) is None
    assert split_href(href) is None
    assert is_absolute_href(href)
    assert not is_catalog_url(href)
    assert not is_https_href(href)
    assert not is_uri_reference(href)
    assert relative_path("roads", href) is None
    assert crawl_prefix("https://h/", "roads", href) is None
    assert live_host_href("roads", href, "https://h/") is None
    assert live_host_href("roads", "./a", href) is None
    assert resolve_live_base(None, None, href) == (None, False)
    assert location_key(href) is None
    assert object_location(href) is None
    assert object_location("./a", base=href) is None
    with pytest.raises(ValueError, match="catalog URL must be https"):
        split_catalog_url(href)
    with pytest.raises(ValueError, match="live base_url must be an https URL"):
        normalize_live_base(href)
    assert location_key("https://h:bad/a") is None


def test_local_resolution_decodes_once_and_keeps_request_spelling() -> None:
    from rashid._href import local_path, request_target

    directory = "my%20%2520%20roads"
    href = "./a%20b%2520.parquet?token=x#piece"
    assert relative_path(directory, href) == "my%20%2520%20roads/a%20b%2520.parquet"
    assert local_path(directory, href) == "my %20 roads/a b%20.parquet"
    assert request_target(directory, href) == (
        "my%20%2520%20roads/a%20b%2520.parquet?token=x#piece"
    )
    for unsafe in ("%2e%2e/%2e%2e/outside", "%2Foutside", "a%5Cb", "a%00b"):
        assert local_path("roads", unsafe) is None
        assert request_target("roads", unsafe) == "roads/" + unsafe
