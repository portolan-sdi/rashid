"""Version-chain regressions through the CLI, with real graph loading."""

from __future__ import annotations

import json
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from click.testing import CliRunner

from rashid.cli import main
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json

pytestmark = pytest.mark.unit

_VERSION = "https://stac-extensions.github.io/version/v1.2.0/schema.json"
_RULE = "PTL-VER-001"


def _chain(catalog: CatalogBuilder) -> Path:
    collection = catalog.collection("releases")
    for name in ("old", "new"):
        collection.item(
            name,
            stac_extensions=[_VERSION],
            properties={"version": name, "datetime": "2024-01-01T00:00:00Z"},
        )
    root = catalog.write()
    _link(root, "old", "successor-version", "../new/new.json")
    _link(root, "new", "predecessor-version", "../old/old.json")
    _link(root, "collection", "latest-version", "./new/new.json")
    return root


def _path(root: Path, name: str) -> Path:
    return (
        root / "releases" / ("collection.json" if name == "collection" else f"{name}/{name}.json")
    )


def _link(root: Path, name: str, rel: str, href: Any) -> None:
    mutate_json(
        _path(root, name),
        lambda d: d["links"].append({"rel": rel, "href": href}),
    )


def _remove(root: Path, name: str, rel: str) -> None:
    mutate_json(
        _path(root, name),
        lambda d: d.__setitem__("links", [link for link in d["links"] if link["rel"] != rel]),
    )


def _findings(root: Path | str, *args: str) -> list[dict[str, Any]]:
    result = CliRunner().invoke(main, ["check", "--json", "--no-data", *args, str(root)])
    assert result.exit_code in (0, 1), result.output
    return [f for f in json.loads(result.output)["findings"] if f["rule_id"] == _RULE]


@pytest.mark.parametrize("missing", ["predecessor-version", "successor-version"])
def test_version_neighbours_are_reciprocal(catalog: CatalogBuilder, missing: str) -> None:
    root = _chain(catalog)
    name = "new" if missing == "predecessor-version" else "old"
    _remove(root, name, missing)
    findings = _findings(root)
    assert len(findings) == 1
    assert missing in findings[0]["message"]
    assert findings[0]["json_pointer"].endswith("/href")
    assert findings[0]["severity"] == "warning"
    _link(root, name, missing, "../old/old.json" if name == "new" else "../new/new.json")
    assert _findings(root) == []


@pytest.mark.parametrize("self_cycle", [True, False], ids=["self", "two-items"])
def test_version_cycles(catalog: CatalogBuilder, self_cycle: bool) -> None:
    root = _chain(catalog)
    _remove(root, "collection", "latest-version")
    target = "new" if self_cycle else "old"
    _link(root, "new", "successor-version", f"../{target}/{target}.json")
    _link(root, target, "predecessor-version", "../new/new.json")
    assert any("cycle" in f["message"] for f in _findings(root))
    _remove(root, "new", "successor-version")
    _remove(root, target, "predecessor-version")
    if self_cycle:
        _link(root, "new", "predecessor-version", "../old/old.json")
    assert _findings(root) == []


@pytest.mark.parametrize("name", ["collection", "old", "new"])
def test_latest_version_targets_a_head(catalog: CatalogBuilder, name: str) -> None:
    root = _chain(catalog)
    _remove(root, name, "latest-version")
    href = "./old/old.json" if name == "collection" else "../old/old.json"
    _link(root, name, "latest-version", href)
    findings = _findings(root)
    assert len(findings) == 1
    assert "successor" in findings[0]["message"]
    _remove(root, name, "latest-version")
    _link(root, name, "latest-version", href.replace("old", "new"))
    assert _findings(root) == []


@pytest.mark.parametrize("rel", ["predecessor-version", "successor-version", "latest-version"])
@pytest.mark.parametrize(
    "href", ["./absent.json", "../../README.md", "../../../escape.json", "", None, 42]
)
def test_version_targets_are_stac_resources(catalog: CatalogBuilder, rel: str, href: Any) -> None:
    root = _chain(catalog)
    _link(root, "new", rel, href)
    assert any("resolve" in f["message"] for f in _findings(root))
    _remove(root, "new", rel)
    if rel == "predecessor-version":
        _link(root, "new", rel, "../old/old.json")
    assert _findings(root) == []


def test_only_one_latest_link(catalog: CatalogBuilder) -> None:
    root = _chain(catalog)
    _link(root, "collection", "latest-version", "./new/new.json")
    assert any("one latest-version" in f["message"] for f in _findings(root))
    _remove(root, "collection", "latest-version")
    _link(root, "collection", "latest-version", "./new/new.json")
    assert _findings(root) == []


@pytest.mark.parametrize("history", ["../../README.md", "https://example.org/history.md"])
def test_history_can_be_a_changelog(catalog: CatalogBuilder, history: str) -> None:
    root = _chain(catalog)
    _link(root, "new", "version-history", "./missing.md")
    assert any("version-history" in f["message"] for f in _findings(root))
    _remove(root, "new", "version-history")
    _link(root, "new", "version-history", history)
    assert _findings(root) == []


@pytest.mark.parametrize("spelling", ["encoded", "absolute", "suffix", "organized"])
def test_version_href_policies(catalog: CatalogBuilder, spelling: str) -> None:
    root = _chain(catalog)
    source = _path(root, "old")
    href = "../new/new.json"
    if spelling == "encoded":
        destination = source.parent.with_name("old release")
        source.parent.rename(destination)
        source = destination / "old.json"
        _remove(root, "new", "predecessor-version")
        _link(root, "new", "predecessor-version", "../old%20release/old.json")
    elif spelling == "absolute":
        mutate_json(
            root / "catalog.json",
            lambda d: d["links"].append(
                {"rel": "self", "href": "https://example.org/demo/catalog.json"}
            ),
        )
        href = "https://example.org/demo/releases/new/new.json"
    elif spelling == "suffix":
        href += "?token=x#section"
    else:
        directory = root / "releases" / "group"
        directory.mkdir()
        (directory / "catalog.json").write_text(
            json.dumps({"type": "Catalog", "stac_version": "1.1.0", "id": "group", "links": []})
        )
        source.parent.rename(directory / "old")
        source = directory / "old" / "old.json"
        href = "../../new/new.json"
        _remove(root, "new", "predecessor-version")
        _link(root, "new", "predecessor-version", "../group/old/old.json")
    mutate_json(
        source,
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link["rel"] != "successor-version"]
        ),
    )
    assert any("successor-version" in f["message"] for f in _findings(root))
    mutate_json(source, lambda d: d["links"].append({"rel": "successor-version", "href": href}))
    assert _findings(root) == []


def test_branches_merge_with_opaque_and_repeated_labels(catalog: CatalogBuilder) -> None:
    root = _chain(catalog)
    # The extension permits branching, merging, and opaque version labels.
    mutate_json(
        _path(root, "new"),
        lambda d: d["properties"].update({"version": "1999-01-01", "deprecated": True}),
    )
    branch = _path(root, "new").parent.with_name("branch")
    branch.mkdir()
    data = json.loads(_path(root, "new").read_text())
    data["id"] = "branch"
    data["properties"].update({"version": "1999-01-01", "deprecated": True})
    data["links"] = [
        {"rel": "predecessor-version", "href": "../old/old.json"},
        {"rel": "successor-version", "href": "../new/new.json"},
    ]
    (branch / "branch.json").write_text(json.dumps(data))
    _link(root, "old", "successor-version", "../branch/branch.json")
    _link(root, "new", "predecessor-version", "../branch/branch.json")
    assert _findings(root) == []
    _link(root, "new", "successor-version", "../old/old.json")
    _link(root, "old", "predecessor-version", "../new/new.json")
    assert any("cycle" in f["message"] for f in _findings(root))


def test_single_version_may_be_its_own_latest(catalog: CatalogBuilder) -> None:
    catalog.collection("releases").item("new", stac_extensions=[_VERSION])
    root = catalog.write()
    _link(root, "new", "latest-version", "./new.json")
    assert _findings(root) == []
    _link(root, "new", "successor-version", "./new.json")
    _link(root, "new", "predecessor-version", "./new.json")
    assert any("cycle" in f["message"] for f in _findings(root))


def test_collections_can_be_versions(catalog: CatalogBuilder) -> None:
    catalog.collection("old", stac_extensions=[_VERSION])
    catalog.collection("new", stac_extensions=[_VERSION])
    root = catalog.write()
    mutate_json(
        root / "old/collection.json",
        lambda d: d["links"].append({"rel": "successor-version", "href": "../new/collection.json"}),
    )
    assert any("predecessor-version" in f["message"] for f in _findings(root))
    mutate_json(
        root / "new/collection.json",
        lambda d: d["links"].append(
            {"rel": "predecessor-version", "href": "../old/collection.json"}
        ),
    )
    assert _findings(root) == []


def test_long_version_history_is_iterative(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("releases")
    for index in range(1100):
        links = []
        if index:
            links.append(
                {"rel": "predecessor-version", "href": f"../v{index - 1}/v{index - 1}.json"}
            )
        if index < 1099:
            links.append({"rel": "successor-version", "href": f"../v{index + 1}/v{index + 1}.json"})
        collection.item(f"v{index}", stac_extensions=[_VERSION], links=links)
    root = catalog.write()
    assert _findings(root) == []
    _link(root, "v1099", "successor-version", "../v0/v0.json")
    _link(root, "v0", "predecessor-version", "../v1099/v1099.json")
    assert any("cycle" in f["message"] for f in _findings(root))


@pytest.mark.parametrize("rel", ["predecessor-version", "successor-version", "latest-version"])
def test_external_versions_need_network_evidence(catalog: CatalogBuilder, rel: str) -> None:
    root = _chain(catalog)
    _link(root, "new", rel, "https://external.example.org/version.json")
    assert _findings(root) == []
    _remove(root, "old", "successor-version")
    assert any("reciprocal successor-version" in f["message"] for f in _findings(root))


@pytest.mark.parametrize("history", [None, "", "./absent.md"])
def test_history_links_resolve(catalog: CatalogBuilder, history: Any) -> None:
    root = _chain(catalog)
    _link(root, "new", "version-history", history)
    assert any("version-history does not resolve" in f["message"] for f in _findings(root))
    _remove(root, "new", "version-history")
    _link(root, "new", "version-history", "../collection.json")
    assert _findings(root) == []


def test_unparseable_version_target(catalog: CatalogBuilder) -> None:
    root = _chain(catalog)
    bad = root / "bad"
    bad.mkdir()
    (bad / "collection.json").write_text("{")
    _link(root, "new", "successor-version", "../../bad/collection.json")
    assert any("does not resolve" in f["message"] for f in _findings(root))
    _remove(root, "new", "successor-version")
    assert _findings(root) == []


def test_version_pointer_preserves_original_link_index(catalog: CatalogBuilder) -> None:
    root = _chain(catalog)
    mutate_json(_path(root, "new"), lambda d: d["links"].insert(0, None))
    _link(root, "new", "successor-version", "./absent.json")
    findings = _findings(root)
    assert len(findings) == 1
    assert findings[0]["json_pointer"] == "/links/5/href"


def test_remote_history_is_not_checked_against_crawl_mirror(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    root = _chain(catalog)
    _link(root, "collection", "version-history", "./CHANGELOG.md")
    (root / "releases/CHANGELOG.md").write_text("# Changes\n")
    for path in root.rglob("*"):
        if path.is_file():
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )
    assert _findings(https_server.url("catalog.json"), "--no-live") == []
    assert not any(request.path.endswith("CHANGELOG.md") for request in https_server.seen)


@pytest.mark.parametrize("broken", ["predecessor-version", "successor-version"])
@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath], ids=["posix", "windows"])
def test_unresolved_partner_does_not_add_reciprocal_warning(
    catalog: CatalogBuilder, broken: str, path_type: type[PurePath]
) -> None:
    root = _chain(catalog)
    name = "new" if broken == "predecessor-version" else "old"
    _remove(root, name, broken)
    _link(root, name, broken, "./absent.json")
    findings = _findings(root)
    assert len(findings) == 1
    assert findings[0]["message"] == f"{broken} does not resolve to a STAC resource"
    expected_path = path_type(_path(root, name)).relative_to(path_type(root))
    assert findings[0]["path"] == expected_path.as_posix()

    # A genuinely absent partner still needs its own diagnostic.
    _remove(root, name, broken)
    findings = _findings(root)
    assert len(findings) == 1
    assert f"no reciprocal {broken}" in findings[0]["message"]

    # An unresolved link on another node/relation does not suppress it.
    other = "old" if name == "new" else "new"
    _link(root, other, broken, "./absent.json")
    findings = _findings(root)
    assert len(findings) == 2
    assert any(f"no reciprocal {broken}" in f["message"] for f in findings)


@pytest.mark.parametrize("rel", ["predecessor-version", "successor-version", "latest-version"])
def test_remote_versions_only_check_loaded_targets(
    catalog: CatalogBuilder, https_server: HttpsServer, rel: str
) -> None:
    root = _chain(catalog)
    _remove(root, "old", "successor-version")
    _remove(root, "new", "predecessor-version")
    _link(root, "new", rel, "../old/old.json")
    collection_path = _path(root, "collection")
    linked_collection = collection_path.read_bytes()
    mutate_json(
        collection_path,
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link.get("href") != "./old/old.json"]
        ),
    )
    for path in root.rglob("*"):
        if path.is_file():
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )

    location = https_server.url("catalog.json")
    assert _findings(location, "--no-live") == []
    assert not any(request.path == "/releases/old/old.json" for request in https_server.seen)

    # Both endpoints are now reachable through containment, so reciprocity applies.
    https_server.routes["/releases/collection.json"] = Route(body=linked_collection)
    _remove(root, "new", rel)
    _link(root, "new", "predecessor-version", "../old/old.json")
    https_server.routes["/releases/new/new.json"] = Route(body=_path(root, "new").read_bytes())
    findings = _findings(location, "--no-live")
    assert len(findings) == 1
    assert "no reciprocal successor-version" in findings[0]["message"]
    assert any(request.path == "/releases/old/old.json" for request in https_server.seen)


@pytest.mark.parametrize("inverse", ["predecessor-version", "successor-version"])
def test_unjudgeable_absolute_inverse_is_not_missing(catalog: CatalogBuilder, inverse: str) -> None:
    root = _chain(catalog)
    name, target = ("new", "old") if inverse == "predecessor-version" else ("old", "new")
    _remove(root, name, inverse)
    _link(root, name, inverse, f"https://example.org/releases/{target}/{target}.json")
    assert _findings(root) == []

    _remove(root, name, inverse)
    findings = _findings(root)
    assert len(findings) == 1
    assert f"no reciprocal {inverse}" in findings[0]["message"]


def test_remote_latest_cardinality_includes_unloaded_targets(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    root = _chain(catalog)
    _remove(root, "new", "predecessor-version")
    _link(root, "new", "latest-version", "../old/old.json")
    _link(root, "new", "latest-version", "../old/old.json")
    mutate_json(
        _path(root, "collection"),
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link.get("href") != "./old/old.json"]
        ),
    )
    for path in root.rglob("*"):
        if path.is_file():
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )
    findings = _findings(https_server.url("catalog.json"), "--no-live")
    assert len(findings) == 1
    assert findings[0]["message"] == "at most one latest-version link is allowed"
    assert findings[0]["path"] == "releases/new/new.json"
    assert findings[0]["json_pointer"] == "/links"
    assert not any(request.path == "/releases/old/old.json" for request in https_server.seen)


def test_multiple_back_edges_report_one_cycle_per_origin(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("releases")
    for name in ("a", "b", "c"):
        collection.item(name, stac_extensions=[_VERSION])
    root = catalog.write()
    for older, newer in (("a", "b"), ("b", "c"), ("c", "a"), ("c", "b")):
        _link(root, older, "successor-version", f"../{newer}/{newer}.json")
        _link(root, newer, "predecessor-version", f"../{older}/{older}.json")
    findings = _findings(root)
    assert [(f["path"], f["message"]) for f in findings] == [
        ("releases/c/c.json", "version history contains a cycle")
    ]
