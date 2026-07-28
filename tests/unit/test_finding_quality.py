"""The finding-quality gate: every ERROR is actionable without reading prose.

The downstream consumer renders a finding as a requirement an agent has to
satisfy, so an ERROR that says only what is wrong costs a round trip. This
module builds a deliberately broken tree that trips as many ERROR-severity
rules as one corpus can reach, then asserts that every ERROR finding it
produces carries an imperative ``fix_hint`` and a ``json_pointer`` locating
the defect.

Two documented tables carry the exceptions, following ``_SEVERITY_EXCEPTIONS``
in ``test_spec_coverage.py``: each entry says why the field is meaningless for
that rule, and an entry that stops being exercised fails the gate, so neither
table can rot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rashid import validate
from rashid.model import Finding, Severity
from tests.conftest import (
    CatalogBuilder,
    default_asset,
    mutate_json,
    thumbnail_asset,
)

pytestmark = pytest.mark.unit

# ERROR rules whose defect has no location inside a JSON document.
_NO_POINTER: dict[str, str] = {
    # The root catalog.json is missing or unreadable: there is no document to
    # point into.
    "PTL-GEN-000": "the file the pointer would index does not exist",
    # The file is not JSON, so no pointer can be resolved against it.
    "PTL-GEN-001": "the file does not parse, so no pointer resolves",
    # A required file is absent from a directory; the defect is the directory
    # listing, not a field of the object that reports it.
    "PTL-FIL-001": "the defect is a missing file, not a field",
    # README.md is Markdown; its emptiness and its missing title heading are
    # defects of prose, not of the STAC object the finding is filed against.
    "PTL-FIL-004": "the defect is in README.md, which has no JSON structure",
    # A nested collection is wrong because of where its file sits in the tree;
    # no field of the collection carries the defect.
    "PTL-COL-002": "the defect is the file's place in the tree, not a field",
}

# ERROR rules that report a file the validator could not read at all. Nothing
# in the metadata is trustworthy at that point, so there is no edit to name.
_NO_HINT: dict[str, str] = {
    "PTL-GEN-000": "no catalog was found to advise on",
    "PTL-GEN-001": "the file's contents are unknown until it parses",
}

# The ERROR rules this corpus is built to trip. Listed so that a fixture that
# stops reaching one fails here rather than quietly shrinking the gate.
_COVERED: frozenset[str] = frozenset(
    {
        "PTL-AST-001",
        "PTL-AST-002",
        "PTL-AST-003",
        "PTL-AST-004",
        "PTL-AST-005",
        "PTL-BBX-001",
        "PTL-CNF-001",
        "PTL-CNF-003",
        "PTL-COL-001",
        "PTL-COL-002",
        "PTL-COL-004",
        "PTL-FIL-001",
        "PTL-FIL-002",
        "PTL-FIL-003",
        "PTL-FIL-004",
        "PTL-GEN-000",
        "PTL-GEN-001",
        "PTL-LIC-001",
        "PTL-LIC-002",
        "PTL-LIC-003",
        "PTL-LNK-001",
        "PTL-LNK-002",
        "PTL-LNK-003",
        "PTL-LNK-004",
        "PTL-LNK-005",
        "PTL-LNK-006",
        "PTL-MIR-002",
        "PTL-PRO-001",
        "PTL-PRO-003",
        "PTL-PRO-004",
        "PTL-PRT-001",
        "PTL-PRV-001",
        "PTL-PRV-002",
        "PTL-PRV-003",
        "PTL-TMP-002",
        "PTL-TTL-001",
        "PTL-TTL-003",
        "PTL-VIZ-001",
        "PTL-VIZ-002",
        "PTL-VIZ-003",
        "PTL-VIZ-005",
    }
)

_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_PMTILES_TYPE = "application/vnd.pmtiles"


def _asset(**overrides: Any) -> dict[str, Any]:
    asset = default_asset()
    asset.update(overrides)
    return asset


def _add_collections(catalog: CatalogBuilder) -> None:
    """Collections whose defects are expressible as builder overrides."""
    # Two hosts, no producer; an s3 href with no roles and a raw digest.
    kitchen = catalog.collection(
        "bad_layer",
        title="ns:bad_layer",
        license="proprietary",
        providers=[{"name": "A", "roles": ["host"]}, {"name": "B", "roles": ["host"]}],
        assets={
            "data": {
                "href": "s3://bucket/data.parquet",
                "type": "application/vnd.apache.parquet",
                "file:checksum": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8",
            }
        },
    )
    kitchen.item("bad-item", properties={"datetime": "yesterday"})

    # A mirror (producer and host differ) with no via link, no updated field,
    # and a host reachable neither by url nor by email.
    catalog.collection(
        "mirror-gap",
        license="other",
        providers=[
            {"name": "Source Org", "roles": ["producer"], "url": "https://source.example.org"},
            {"name": "Mirror Org", "roles": ["host"]},
        ],
    )

    # An official collection carrying an upstream link, and a version field
    # without the version extension.
    catalog.collection("official-via", version="2")

    # No title, no description, no license, and no providers.
    catalog.collection("titleless", title="", description="", license="", providers=[])

    # A visual derivative with no style asset, and no thumbnail.
    catalog.collection(
        "visual-only",
        assets={"data": default_asset(), "preview": _asset(roles=["visual"])},
    )

    # PMTiles registered wrongly: bad link type, no layers, extension
    # undeclared, style asset typed as plain JSON.
    catalog.collection(
        "tiles",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "tiles": _asset(href="./tiles.pmtiles", type=_PMTILES_TYPE, roles=["visual"]),
            "style": _asset(href="./styles/default.json", type="application/json", roles=["style"]),
        },
    )

    # partition:scheme alone: keys, glob, and the extension are all missing.
    catalog.collection("parts", **{"partition:scheme": "hive"})

    # Two scene COGs at collection level.
    catalog.collection(
        "scenes",
        assets={
            "thumbnail": thumbnail_asset(),
            "a": _asset(href="./a.tif", type=_COG_TYPE),
            "b": _asset(href="./b.tif", type=_COG_TYPE),
        },
    )

    # An items.parquet mirror carrying neither the role nor the media type.
    catalog.collection(
        "mirror-asset",
        assets={
            "data": default_asset(),
            "items": _asset(href="./items.parquet", type="application/json", roles=["metadata"]),
        },
    )

    # The collection's only data file lives on its only item.
    single = catalog.collection("single-file", assets={"thumbnail": thumbnail_asset()})
    single.item("only-item")


def _mutate_collections(root: Path) -> None:
    """Defects that only exist once the tree is on disk."""
    (root / "bad_layer" / "AGENTS.md").unlink()
    mutate_json(
        root / "bad_layer" / "collection.json",
        lambda d: (
            d.pop("stac_extensions"),
            d["extent"]["spatial"].__setitem__("bbox", [[4.0, 52.0, 6.0, 50.0]]),
            d.__setitem__("links", [ln for ln in d["links"] if ln["rel"] != "parent"]),
        ),
    )
    mutate_json(
        root / "mirror-gap" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "describedby", "href": "https://example.org/README.md", "type": "text/plain"}
        ),
    )
    mutate_json(
        root / "official-via" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "via", "href": "https://example.org/source", "type": "text/html"}
        ),
    )
    mutate_json(
        root / "titleless" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "agents", "href": "./MISSING.md", "type": "text/markdown"}
        ),
    )
    mutate_json(
        root / "tiles" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "pmtiles", "href": "./tiles.pmtiles", "type": "application/json"}
        ),
    )
    (root / "single-file" / "README.md").write_text("", encoding="utf-8")


def _mutate_root(root: Path) -> None:
    """Root-catalog defects: a self link, a bad child link, a stray asset."""

    def untitle(links: list[dict[str, Any]]) -> None:
        for link in links:
            if link.get("href") == "./titleless/collection.json":
                link.pop("title", None)

    mutate_json(
        root / "catalog.json",
        lambda d: (
            untitle(d["links"]),
            d["links"].append(
                {"rel": "self", "href": "./catalog.json", "type": "application/json"}
            ),
            d["links"].append(
                {
                    "rel": "child",
                    "href": "https://example.org/absent/collection.json",
                    "type": "text/plain",
                    "title": "Absent",
                }
            ),
            d["links"].append(
                {
                    "rel": "child",
                    "href": "./gone/collection.json",
                    "type": "application/json",
                    "title": "Gone",
                }
            ),
            d.__setitem__("assets", {"stray": default_asset()}),
            d.__setitem__(
                "links",
                [ln for ln in d["links"] if ln.get("href") != "./scenes/collection.json"],
            ),
        ),
    )


def _add_nested_and_unparseable(root: Path) -> None:
    """A collection nested in a collection, and a file that is not JSON."""
    nested = root / "single-file" / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "collection.json").write_text(
        json.dumps(
            {
                "type": "Collection",
                "stac_version": "1.1.0",
                "id": "nested",
                "title": "Nested Collection",
                "description": "A collection where the spec allows none.",
                "license": "CC-BY-4.0",
                "stac_extensions": ["https://schemas.portolan-sdi.org/portolan/v0.1.0/schema.json"],
                "extent": {
                    "spatial": {"bbox": [[4.0, 50.0, 6.0, 52.0]]},
                    "temporal": {"interval": [[None, None]]},
                },
                "providers": [
                    {"name": "Demo Org", "roles": ["producer", "host"], "url": "https://e.org"}
                ],
                "links": [
                    {"rel": "root", "href": "../../catalog.json", "type": "application/json"},
                    {"rel": "parent", "href": "../collection.json", "type": "application/json"},
                    {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown"},
                    {"rel": "describedby", "href": "./README.md", "type": "text/markdown"},
                ],
                "assets": {"data": default_asset(), "thumbnail": thumbnail_asset()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (nested / "AGENTS.md").write_text("# Agents\n\nRelative hrefs.\n", encoding="utf-8")
    (nested / "README.md").write_text("# Nested\n\nLicense: CC-BY-4.0, source: us.\n", "utf-8")

    broken = root / "unparseable"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "collection.json").write_text("{not json", encoding="utf-8")
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {
                "rel": "child",
                "href": "./unparseable/collection.json",
                "type": "application/json",
                "title": "Unparseable",
            }
        ),
    )


def broken_tree(tmp_path: Path) -> Path:
    """A catalog built to trip every rule id in ``_COVERED``."""
    catalog = CatalogBuilder(tmp_path / "catalog")
    _add_collections(catalog)
    root = catalog.write()
    _mutate_collections(root)
    _mutate_root(root)
    _add_nested_and_unparseable(root)
    return root


def error_findings(tmp_path: Path) -> list[Finding]:
    """Every ERROR finding the corpus produces.

    Two trees, because a catalog cannot be both present and absent: the broken
    tree for the rules, and an empty directory for the missing-root report.
    """
    findings = list(validate(broken_tree(tmp_path), structural=False).findings)
    empty = tmp_path / "empty"
    empty.mkdir()
    findings.extend(validate(empty, structural=False).findings)
    return [f for f in findings if f.severity is Severity.ERROR]


@pytest.fixture(scope="module")
def errors(tmp_path_factory: pytest.TempPathFactory) -> list[Finding]:
    return error_findings(tmp_path_factory.mktemp("finding-quality"))


def test_every_error_carries_a_fix_hint(errors: list[Finding]) -> None:
    missing = sorted(
        {f.rule_id for f in errors if f.rule_id not in _NO_HINT and not (f.fix_hint or "").strip()}
    )
    assert not missing, f"ERROR findings without a fix_hint: {missing}"


def test_every_error_carries_a_json_pointer(errors: list[Finding]) -> None:
    missing = sorted(
        {
            f.rule_id
            for f in errors
            if f.rule_id not in _NO_POINTER and not (f.json_pointer or "").strip()
        }
    )
    assert not missing, f"ERROR findings without a json_pointer: {missing}"


def test_exception_tables_are_exercised(errors: list[Finding]) -> None:
    """An exception nobody needs any more must be deleted, not left to rot."""
    reached = {f.rule_id for f in errors}
    assert not (set(_NO_POINTER) - reached), "stale _NO_POINTER entries"
    assert not (set(_NO_HINT) - reached), "stale _NO_HINT entries"
    pointered = {f.rule_id for f in errors if (f.json_pointer or "").strip()}
    assert not (set(_NO_POINTER) & pointered), "_NO_POINTER entries that now carry a pointer"
    hinted = {f.rule_id for f in errors if (f.fix_hint or "").strip()}
    assert not (set(_NO_HINT) & hinted), "_NO_HINT entries that now carry a hint"


def test_corpus_reaches_every_covered_rule(errors: list[Finding]) -> None:
    assert _COVERED - {f.rule_id for f in errors} == set()


def test_hints_are_imperative(errors: list[Finding]) -> None:
    """A hint names an edit; it does not restate the defect."""
    restatements = sorted(
        {
            f"{f.rule_id}: {f.fix_hint}"
            for f in errors
            if f.fix_hint and f.fix_hint.strip().split(" ", 1)[0].lower() in _NON_IMPERATIVE
        }
    )
    assert not restatements, f"fix_hints that describe rather than instruct: {restatements}"


# Openers that mark a hint as a restatement of the message rather than an edit.
_NON_IMPERATIVE = frozenset({"the", "this", "it", "there", "no", "missing", "a", "an"})
