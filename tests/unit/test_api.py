"""Tests for the rashid_core.api surface.

These tests check what the module exports and that each export still
resolves to its implementation. The behaviour of each helper is tested in
the module that defines it.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

import rashid_core
from rashid_core import api, validate
from rashid_core.catalog import CatalogGraph, Node, expected_links
from rashid_core.model import Finding, Severity
from rashid_core.rule import Rule
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit

# Every exported name, paired with the module that defines it. This catches
# a re-export that no longer resolves to its implementation.
EXPORTS = {
    "CatalogGraph": "rashid_core.catalog",
    "ExpectedLink": "rashid_core.catalog",
    "LocalOnlyReader": "rashid_core.data.reader",
    "Node": "rashid_core.catalog",
    "Report": "rashid_core.report",
    "RulesConfig": "rashid_core.config",
    "SHA2_256": "rashid_core._multihash",
    "SPDX_LICENSE_IDS": "rashid_core._spdx",
    "STRUCTURAL_RELS": "rashid_core._stac",
    "Severity": "rashid_core.model",
    "ValidationOptions": "rashid_core.runner",
    "bundled_profile_versions": "rashid_core._profile",
    "canonical_spdx_id": "rashid_core._spdx",
    "decode_multihash": "rashid_core._multihash",
    "encode_multihash": "rashid_core._multihash",
    "expected_links": "rashid_core.catalog",
    "has_cog": "rashid_core._media",
    "is_absolute_href": "rashid_core._href",
    "is_cog_media_type": "rashid_core._media",
    "is_well_formed_multihash": "rashid_core._multihash",
    "links_of": "rashid_core._stac",
    "newest_profile_version": "rashid_core._profile",
    "parse_rfc3339": "rashid_core._stac",
    "profile_uri": "rashid_core._profile",
    "profile_version": "rashid_core._profile",
    "roles_of": "rashid_core._stac",
    "spec_commit": "rashid_core._profile",
    "profile_version_key": "rashid_core._profile",
    "CHECKS": "rashid_core.registry",
    "CheckInfo": "rashid_core.registry",
    "Finding": "rashid_core.model",
    "SidecarLinkGap": "rashid_core._stac",
    "describe": "rashid_core.registry",
    "is_pmtiles_asset": "rashid_core._media",
    "resolve_local_href": "rashid_core._href",
    "sidecar_link_gaps": "rashid_core._stac",
    "validate": "rashid_core.runner",
}

# Names portolan-cli needs to import rashid only through rashid_core.api
# (portolan-cli#1133).
DOWNSTREAM_EXPORTS = (
    "CHECKS",
    "CheckInfo",
    "Finding",
    "SidecarLinkGap",
    "describe",
    "is_pmtiles_asset",
    "resolve_local_href",
    "sidecar_link_gaps",
    "validate",
)


def _node(data: dict[str, object]) -> Node:
    return Node(
        path=PurePosixPath("collection.json"),
        abs_path=Path("/tmp/collection.json"),
        kind="collection",
        id="test",
        data=data,
    )


def test_all_lists_every_documented_export() -> None:
    assert sorted(api.__all__) == sorted(EXPORTS)


@pytest.mark.parametrize("module", [api, rashid_core], ids=["rashid_core.api", "rashid_core"])
def test_public_api_snapshot(module: ModuleType) -> None:
    snapshot = json.loads(Path(__file__).with_name("api_snapshot.json").read_text(encoding="utf-8"))
    expected = snapshot[module.__name__]
    assert sorted(module.__all__) == expected["exports"]
    assert {
        name: getattr(module, name).__module__
        for name in sorted(module.__all__)
        if callable(getattr(module, name))
    } == expected["modules"]
    assert {
        name: str(inspect.signature(getattr(module, name)))
        for name in sorted(module.__all__)
        # Enum's constructor belongs to Python and changes between Python versions.
        if callable(getattr(module, name)) and name != "Severity"
    } == expected["signatures"]


@pytest.mark.parametrize("include_rule", [True, False])
def test_options_use_the_custom_rule_set(catalog: CatalogBuilder, include_rule: bool) -> None:
    class CustomRule(Rule):
        id = "CUSTOM-001"
        default_severity = Severity.INFO
        description = "custom catalog check"

        def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
            assert graph.root is not None
            yield self.finding(graph.root, "custom rule ran")

    report = validate(
        catalog.write(),
        options=api.ValidationOptions(
            rules=[CustomRule()] if include_rule else [],
            structural=False,
            extensions=False,
            data=False,
        ),
    )

    assert [f.rule_id for f in report.findings] == (["CUSTOM-001"] if include_rule else [])


@pytest.mark.parametrize("name", sorted(EXPORTS))
def test_export_is_the_defining_module_s_object(name: str) -> None:
    source = importlib.import_module(EXPORTS[name])
    assert getattr(api, name) == getattr(source, name)


def test_structural_rels_covers_the_navigable_tree() -> None:
    assert set(api.STRUCTURAL_RELS) == {"root", "parent", "child", "item", "collection"}


def test_cog_media_type_needs_the_profile_parameter() -> None:
    assert api.is_cog_media_type("image/tiff; application=geotiff; profile=cloud-optimized")


def test_plain_geotiff_is_not_a_cog() -> None:
    # the bug this export exists to prevent, matching the prefix alone
    assert not api.is_cog_media_type("image/tiff; application=geotiff")


def test_cog_media_type_ignores_case_and_surrounding_space() -> None:
    assert api.is_cog_media_type("  IMAGE/TIFF; PROFILE=CLOUD-OPTIMIZED; APPLICATION=GEOTIFF  ")


def test_cog_media_type_needs_the_geotiff_parameter() -> None:
    assert not api.is_cog_media_type("image/tiff; profile=cloud-optimized")


def test_profile_on_a_non_tiff_type_is_not_a_cog() -> None:
    assert not api.is_cog_media_type("image/png; profile=cloud-optimized")


@pytest.mark.parametrize("value", [None, 42, ["image/tiff; profile=cloud-optimized"]])
def test_cog_media_type_tolerates_non_strings(value: object) -> None:
    assert not api.is_cog_media_type(value)


def test_encoded_sha256_carries_the_expected_prefix() -> None:
    digest = hashlib.sha256(b"portolan").digest()
    assert api.encode_multihash(api.SHA2_256, digest) == "1220" + digest.hex()


def test_encode_round_trips_through_decode() -> None:
    digest = hashlib.sha512(b"portolan").digest()
    decoded = api.decode_multihash(api.encode_multihash(0x13, digest))
    assert decoded == (0x13, digest)


def test_encoded_multihash_is_well_formed() -> None:
    encoded = api.encode_multihash(api.SHA2_256, hashlib.sha256(b"portolan").digest())
    assert api.is_well_formed_multihash(encoded)


def test_multi_byte_varints_round_trip() -> None:
    # a code above 0x7f spills into a second varint byte on both sides
    digest = b"\xab" * 200
    assert api.decode_multihash(api.encode_multihash(0x1053, digest)) == (0x1053, digest)


def test_encoding_an_empty_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="digest is empty"):
        api.encode_multihash(api.SHA2_256, b"")


def test_encoding_a_negative_code_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        api.encode_multihash(-1, b"\xab")


def test_parse_rfc3339_reads_an_offset_aware_timestamp() -> None:
    parsed = api.parse_rfc3339("2026-07-28T12:00:00Z")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [None, 20260728, "2026-07-28T12:00:00", "not a date"])
def test_parse_rfc3339_returns_none_for_anything_else(value: object) -> None:
    assert api.parse_rfc3339(value) is None


def test_has_cog_finds_a_cog_asset() -> None:
    node = _node(
        {
            "assets": {
                "thumb": {"type": "image/png"},
                "scene": {"type": "image/tiff; application=geotiff; profile=cloud-optimized"},
            }
        }
    )
    assert api.has_cog(node)


def test_has_cog_rejects_a_collection_of_plain_geotiffs() -> None:
    node = _node({"assets": {"source": {"type": "image/tiff; application=geotiff"}}})
    assert not api.has_cog(node)


def test_has_cog_tolerates_a_missing_assets_field() -> None:
    assert not api.has_cog(_node({}))


def test_canonical_spdx_id_returns_the_official_spelling() -> None:
    assert api.canonical_spdx_id("apache-2.0") == "Apache-2.0"


def test_canonical_spdx_id_passes_an_exact_identifier_through() -> None:
    assert api.canonical_spdx_id("CC-BY-4.0") == "CC-BY-4.0"


def test_canonical_spdx_id_returns_none_for_a_non_identifier() -> None:
    # the near miss PTL-LIC-001 cannot help with: a name, not a spelling
    assert api.canonical_spdx_id("Apache 2.0") is None


@pytest.mark.parametrize("value", [None, 42, ["MIT"]])
def test_canonical_spdx_id_tolerates_non_strings(value: object) -> None:
    assert api.canonical_spdx_id(value) is None


def test_the_exported_list_carries_identifiers_beyond_the_popular_ones() -> None:
    # the downstream case in portolan-cli#727: a real identifier that a
    # hand-maintained shortlist omits
    assert "EUPL-1.2" in api.SPDX_LICENSE_IDS
    assert "OGL-UK-3.0" in api.SPDX_LICENSE_IDS


def test_the_exported_list_holds_no_licenseref_entries() -> None:
    # downstream tools gate on this: LicenseRef-* is an SPDX expression
    # construct, not an identifier, so PTL-LIC-001 rejects every one
    assert [i for i in api.SPDX_LICENSE_IDS if i.casefold().startswith("licenseref")] == []


def test_the_exported_list_excludes_the_stac_escape_hatch() -> None:
    # "other" is a STAC value, not SPDX; a caller has to allow it separately
    assert "other" not in api.SPDX_LICENSE_IDS


def test_downstream_names_are_public() -> None:
    assert set(DOWNSTREAM_EXPORTS) <= set(api.__all__)


@pytest.mark.parametrize("name", ["CHECKS", "CheckInfo", "Finding", "describe", "validate"])
def test_api_and_package_export_the_same_object(name: str) -> None:
    assert getattr(api, name) is getattr(rashid_core, name)


@pytest.mark.parametrize(
    "asset",
    [
        {"href": "./tiles.pmtiles"},
        {"href": "https://example.com/TILES.PMTILES?sig=abc#z"},
        {"type": "application/vnd.pmtiles"},
    ],
)
def test_pmtiles_asset_by_type_or_href(asset: dict[str, object]) -> None:
    assert api.is_pmtiles_asset(asset)


@pytest.mark.parametrize(
    "asset",
    [{"href": "./data.parquet"}, {"href": "./tiles.pmtiles.json"}, {"href": 42}, {}],
)
def test_other_assets_are_not_pmtiles(asset: dict[str, object]) -> None:
    assert not api.is_pmtiles_asset(asset)


@pytest.mark.parametrize(
    ("document", "href", "resolved"),
    [
        ("collection.json", "./README.md", "README.md"),
        ("a/collection.json", "../b/item.json", "b/item.json"),
        ("a b/collection.json", "./my%20file.parquet?x=1#y", "a b/my file.parquet"),
        (PurePosixPath("a/collection.json"), "README.md", "a/README.md"),
    ],
)
def test_resolve_local_href_returns_a_decoded_root_relative_path(
    document: str | PurePosixPath, href: str, resolved: str
) -> None:
    assert api.resolve_local_href(document, href) == PurePosixPath(resolved)


@pytest.mark.parametrize(
    "href", ["", "https://example.com/a.json", "/abs/a.json", "../../out.json", "a%5Cb.json"]
)
def test_resolve_local_href_refuses_hrefs_outside_the_tree(href: str) -> None:
    assert api.resolve_local_href("a/collection.json", href) is None


def _sidecar_gaps(root: Path, rel: str, target: str) -> tuple[api.SidecarLinkGap, ...]:
    graph = CatalogGraph.load(root)
    assert graph.root is not None
    return api.sidecar_link_gaps(graph, graph.root, rel=rel, target=target)


@pytest.mark.parametrize(("rel", "target"), [("agents", "AGENTS.md"), ("describedby", "README.md")])
def test_conforming_sidecar_link_has_no_gaps(
    catalog: CatalogBuilder, rel: str, target: str
) -> None:
    assert _sidecar_gaps(catalog.write(), rel, target) == ()


def test_missing_sidecar_link_is_one_gap(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    data = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    data["links"] = [link for link in data["links"] if link["rel"] != "agents"]
    (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")

    (gap,) = _sidecar_gaps(root, "agents", "AGENTS.md")

    assert gap.message == "missing rel:'agents' link to AGENTS.md"
    assert gap.json_pointer == "/links"


def test_sidecar_gaps_name_the_link_that_comes_closest(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    data = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    index, link = next((i, lk) for i, lk in enumerate(data["links"]) if lk["rel"] == "agents")
    link["type"] = "text/plain"
    data["links"].append({"rel": "agents", "href": "https://example.com/x", "type": "text/html"})
    (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")

    (gap,) = _sidecar_gaps(root, "agents", "AGENTS.md")

    assert gap.json_pointer == f"/links/{index}/type"
    assert gap.expected == "text/markdown"
    assert gap.actual == "text/plain"


def test_one_conforming_sidecar_link_is_enough(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    data = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    data["links"].append({"rel": "describedby", "href": "./schema.html", "type": "text/html"})
    (root / "catalog.json").write_text(json.dumps(data), encoding="utf-8")

    assert _sidecar_gaps(root, "describedby", "README.md") == ()


def test_sidecar_link_to_a_missing_file_is_a_gap(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    (root / "README.md").unlink()

    (gap,) = _sidecar_gaps(root, "describedby", "README.md")

    assert "does not resolve to the sibling README.md" in gap.message


def test_graph_from_documents_matches_the_loaded_graph(catalog: CatalogBuilder) -> None:
    catalog.collection("air-quality").item("2020")
    root = catalog.write()
    (root / ".hidden").mkdir()
    (root / ".hidden" / "collection.json").write_bytes(
        (root / "air-quality" / "collection.json").read_bytes()
    )
    files = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    assert ".hidden/collection.json" in files
    documents = {
        name: json.loads((root / name).read_text(encoding="utf-8"))
        for name in files
        if name.endswith(".json")
    }

    built = CatalogGraph.from_documents(root, documents, files=files)
    loaded = CatalogGraph.load(root)

    assert built.nodes == loaded.nodes
    assert built.dir_listing == loaded.dir_listing
    for path, node in loaded.nodes.items():
        assert [
            (e.rel, e.target.path, e.href) for e in expected_links(built, built.nodes[path])
        ] == [(e.rel, e.target.path, e.href) for e in expected_links(loaded, node)]


def _minimal_tree() -> tuple[dict[str, object], dict[str, object]]:
    """A bare root catalog and collection, new for each test."""
    return (
        {"type": "Catalog", "id": "root", "stac_version": "1.1.0", "links": []},
        {"type": "Collection", "id": "c", "stac_version": "1.1.0", "links": []},
    )


def test_graph_from_documents_needs_no_files_on_disk(tmp_path: Path) -> None:
    catalog, collection = _minimal_tree()

    graph = CatalogGraph.from_documents(
        tmp_path / "not-yet-written",
        {"catalog.json": catalog, "c/collection.json": collection, "c/style.json": {}},
    )

    node = graph.nodes[PurePosixPath("c/collection.json")]
    assert sorted(graph.nodes) == [
        PurePosixPath("c/collection.json"),
        PurePosixPath("catalog.json"),
    ]
    assert [(e.rel, e.href) for e in expected_links(graph, node)] == [
        ("root", "../catalog.json"),
        ("parent", "../catalog.json"),
    ]
    assert graph.file_exists(PurePosixPath("c/style.json"))


def test_graph_from_documents_skips_hidden_directories(tmp_path: Path) -> None:
    catalog, collection = _minimal_tree()

    graph = CatalogGraph.from_documents(
        tmp_path,
        {"catalog.json": catalog, ".hidden/collection.json": collection},
        files=[".hidden/README.md"],
    )

    root = graph.nodes[PurePosixPath("catalog.json")]
    assert sorted(graph.nodes) == [PurePosixPath("catalog.json")]
    assert [e.rel for e in expected_links(graph, root)] == ["root"]
    assert PurePosixPath(".hidden") not in graph.dir_listing


@pytest.mark.parametrize("key", ["/catalog.json", "../catalog.json", "a/../../b.json"])
def test_graph_from_documents_refuses_keys_outside_the_tree(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match="relative POSIX path"):
        CatalogGraph.from_documents(tmp_path, {key: {}})


def test_graph_from_documents_marks_a_non_object_catalog(tmp_path: Path) -> None:
    graph = CatalogGraph.from_documents(tmp_path, {"catalog.json": []})

    assert graph.nodes[PurePosixPath("catalog.json")].parse_error == (
        "top-level JSON value is not an object"
    )
