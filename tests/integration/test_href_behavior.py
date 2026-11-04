"""Href and organizing-catalog regressions through the real CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from rashid import _trace, validate
from rashid.api import ValidationOptions
from rashid.cli import main
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json, nest_items_under_organizing_catalog

pytestmark = pytest.mark.integration

_COG = "image/tiff; application=geotiff; profile=cloud-optimized"


def _check(root: Path | str, *flags: str) -> dict[str, Any]:
    result = CliRunner().invoke(
        main, ["check", "--json", *flags, str(root)], catch_exceptions=False
    )
    assert result.exit_code in (0, 1), result.output
    return json.loads(result.output)


@pytest.mark.parametrize(
    "case,rule_id",
    [
        ("single", "PTL-COL-001"),
        ("raster", "PTL-COL-004"),
        ("geometry", "PTL-VIZ-001"),
        ("unknown", "PTL-VIZ-001"),
    ],
)
def test_collection_rules_follow_items_through_organizing_catalogs(
    catalog: CatalogBuilder, case: str, rule_id: str
) -> None:
    assets = {"data": {"href": "./scene.tif", "roles": ["data"], "type": _COG}}
    collection = catalog.collection("roads", assets=assets if case == "raster" else {})
    item = collection.item("one", assets=assets)
    if case == "unknown":
        item.overrides.update(geometry=None)
    root = catalog.write()
    before = [f for f in _check(root, "--no-data")["findings"] if f["rule_id"] == rule_id]
    assert len(before) == 1
    nest_items_under_organizing_catalog(root, root / "roads")
    after = [f for f in _check(root, "--no-data")["findings"] if f["rule_id"] == rule_id]
    assert after == before


@pytest.mark.parametrize("suffix", ["?token=x#piece", "#piece", "?token=x"])
@pytest.mark.parametrize("absolute", [False, True])
def test_structural_containment_ignores_query_and_fragment(
    catalog: CatalogBuilder, suffix: str, absolute: bool
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    if absolute:
        mutate_json(
            root / "catalog.json",
            lambda d: d["links"].append(
                {"rel": "self", "href": "https://h/cat/catalog.json" + suffix}
            ),
        )
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {
                "rel": "child",
                "type": "application/json",
                "title": "Roads",
                "href": (
                    "https://h/cat/roads/collection.json" if absolute else "./roads/collection.json"
                )
                + suffix,
            }
        ),
    )
    report = _check(root, "--no-data")
    # Absolute URLs were previously unjudgeable when a self link had a suffix.
    # A wrong root target forces the policy to prove it actually resolved them.
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-LNK-006"]
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {
                "rel": "root",
                "type": "application/json",
                "href": (
                    "https://h/cat/roads/collection.json" if absolute else "./roads/collection.json"
                )
                + suffix,
            }
        ),
    )
    findings = [f for f in _check(root, "--no-data")["findings"] if f["rule_id"] == "PTL-LNK-006"]
    assert len(findings) == 1
    assert "wrong object" in findings[0]["message"]


@pytest.mark.parametrize("scope", ["all", "local"])
def test_local_asset_containment_ignores_suffix(catalog: CatalogBuilder, scope: str) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": "./data.bin?token=x#piece",
                "roles": ["data"],
                "type": "application/octet-stream",
                "file:size": 2,
            }
        },
    )
    root = catalog.write()
    (root / "roads/data.bin").write_bytes(b"abc")
    findings = _check(root, "--data-scope", scope)["findings"]
    assert [f for f in findings if f["rule_id"] == "PTL-DAT-002"]


def test_crawl_preserves_query_in_request_and_strips_suffix_for_disk(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {
                "rel": "child",
                "type": "application/json",
                "title": "Roads",
                "href": "./roads/collection.json?token=x#piece",
            }
        ),
    )
    # Remove the unsigned child, so following it cannot mask a bad request.
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__(
            "links", [link for link in d["links"] if link.get("href") != "./roads/collection.json"]
        ),
    )
    https_server.routes["/catalog.json"] = Route(body=(root / "catalog.json").read_bytes())
    https_server.routes["/roads/collection.json?token=x"] = Route(
        body=(root / "roads/collection.json").read_bytes()
    )
    report = _check(https_server.url("catalog.json"), "--no-data", "--no-live")
    assert report["files_checked"] == 2
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-LNK-006"]
    assert "/roads/collection.json?token=x" in [s.path for s in https_server.seen]


def test_remote_relative_asset_preserves_query_in_wire_request(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": "./data.bin?token=x#piece",
                "roles": ["data"],
                "type": "application/octet-stream",
                "file:size": 2,
            }
        },
    )
    root = catalog.write()
    for path in (root / "catalog.json", root / "roads/collection.json"):
        https_server.routes["/" + path.relative_to(root).as_posix()] = Route(body=path.read_bytes())
    https_server.routes["/roads/data.bin?token=x"] = Route(body=b"abc")
    report = _check(https_server.url("catalog.json"), "--no-live")
    assert [f for f in report["findings"] if f["rule_id"] == "PTL-DAT-002"]
    assert "/roads/data.bin?token=x" in [s.path for s in https_server.seen]


@pytest.mark.parametrize("location", ["asset", "structural", "nonstructural"])
@pytest.mark.parametrize(
    "href",
    [
        "./bad name.bin",
        "./bad\tname.bin",
        "./bad<name>.bin",
        "./bad\\name.bin",
        "./bad%name.bin",
        "./bad%2x.bin",
        "./café.bin",
        "./bad[name].bin",
    ],
)
def test_invalid_uri_references_are_errors_at_the_href(
    catalog: CatalogBuilder, location: str, href: str
) -> None:
    from urllib.parse import quote

    catalog.collection("roads")
    root = catalog.write()
    path = root / "roads/collection.json"
    if location == "asset":
        pointer = "/assets/data/href"
        mutate_json(path, lambda d: d["assets"]["data"].update(href=href))
    else:
        pointer = "/links/4/href"
        mutate_json(
            path,
            lambda d: d["links"].append(
                {
                    "rel": "child" if location == "structural" else "via",
                    "href": href,
                    "type": "application/json",
                    "title": "Target",
                }
            ),
        )
    findings = [
        f
        for f in _check(root, "--no-data")["findings"]
        if f["rule_id"] == "PTL-LNK-006" and "valid URI reference" in f["message"]
    ]
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"
    assert findings[0]["json_pointer"] == pointer
    assert findings[0]["actual"] == href
    encoded = quote(href, safe="/")
    if location == "asset":
        mutate_json(path, lambda d: d["assets"]["data"].update(href=encoded))
    else:
        mutate_json(path, lambda d: d["links"][-1].update(href=encoded))
    assert not [
        f for f in _check(root, "--no-data")["findings"] if "valid URI reference" in f["message"]
    ]


def test_issue_208_reference_catalog_repro(tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "reference"
    shutil.copytree(Path(__file__).parents[1] / "fixtures/reference-catalog", root)
    assert _check(root, "--no-data")["error_count"] == 0
    mutate_json(
        root / "boundaries/boston-open-space/collection.json",
        lambda d: d["assets"]["data"].update(href="./boston open space.parquet"),
    )
    report = _check(root, "--no-data")
    assert report["error_count"] == 1
    findings = [f for f in report["findings"] if f["severity"] == "error"]
    assert findings[0]["path"] == "boundaries/boston-open-space/collection.json"
    assert findings[0]["json_pointer"] == "/assets/data/href"


@pytest.mark.parametrize("suffixes", [("#one", "#two"), ("?token=x#one", "?token=y#two")])
def test_fragment_aliases_do_not_consume_the_crawl_document_budget(
    catalog: CatalogBuilder, https_server: HttpsServer, suffixes: tuple[str, str]
) -> None:
    catalog.collection("a").item("one")
    root = catalog.write()

    def aliases(data: dict) -> None:
        child = next(link for link in data["links"] if link.get("rel") == "child")
        child["href"] += suffixes[0]
        data["links"].append({**child, "href": "./a/collection.json" + suffixes[1]})

    mutate_json(root / "catalog.json", aliases)
    for path in root.rglob("*"):
        if path.is_file():
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )
    https_server.routes["/a/collection.json" + suffixes[0].split("#", 1)[0]] = Route(
        body=(root / "a/collection.json").read_bytes()
    )
    report = validate(
        https_server.url("catalog.json"),
        options=ValidationOptions(data=False, live=False, max_documents=3),
    )
    assert report.files_checked == 3
    assert (
        sum(
            s.method == "GET" and s.path.startswith("/a/collection.json") for s in https_server.seen
        )
        == 1
    )
    assert not any("stopped at" in f.message for f in report.findings)


@pytest.mark.parametrize(
    "href,wire_path",
    [
        ("./a.parquet?token=x", "/roads/a.parquet?token=x"),
        ("./a.parquet?token=x#f", "/roads/a.parquet?token=x"),
        ("./100%25.json", "/roads/100%25.json"),
        ("a%2Fb.parquet", "/roads/a%2Fb.parquet"),
        ("./a%23b.bin", "/roads/a%23b.bin"),
    ],
)
def test_live_keeps_encoded_asset_identity_and_uses_signed_crawl_status(
    catalog: CatalogBuilder, https_server: HttpsServer, href: str, wire_path: str
) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": href,
                "type": "application/octet-stream",
                "roles": ["data"],
                "file:size": 3,
            }
        },
    )
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: next(link for link in d["links"] if link.get("rel") == "child").update(
            href="./roads/collection.json?token=x"
        ),
    )
    for path in root.rglob("*"):
        if path.is_file() and path.name != "collection.json":
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )
    https_server.routes["/roads/collection.json?token=x"] = Route(
        body=(root / "roads/collection.json").read_bytes()
    )
    https_server.routes[wire_path] = Route(body=b"abc")
    with _trace.counting() as counts:
        report = validate(
            https_server.url("catalog.json"), options=ValidationOptions(data=False, live=True)
        )
    probes = [
        seen
        for seen in https_server.seen
        if seen.method in ("HEAD", "OPTIONS") or "range" in seen.headers
    ]
    assert any(seen.method == "HEAD" and seen.path == wire_path for seen in probes)
    assert any(seen.method == "GET" and seen.path == wire_path for seen in probes)
    assert not any("collection.json" in seen.path for seen in probes)
    assert not any(f.rule_id == "PTL-LIV-006" for f in report.findings)
    assert counts.get("live.head_cache_hits", 0) > 0

    # Absolute URI paths keep their declared reserved characters.
    absolute_path = "/roads/absolute;a:b@c.bin?token=x"
    mutate_json(
        root / "roads/collection.json",
        lambda d: d["assets"]["data"].update(href=https_server.base + absolute_path),
    )
    https_server.routes["/roads/collection.json?token=x"] = Route(
        body=(root / "roads/collection.json").read_bytes()
    )
    https_server.routes[absolute_path] = Route(body=b"abc")
    validate(https_server.url("catalog.json"), options=ValidationOptions(data=False, live=True))
    assert any(seen.method == "HEAD" and seen.path == absolute_path for seen in https_server.seen)
    assert any(
        seen.method == "GET" and "range" in seen.headers and seen.path == absolute_path
        for seen in https_server.seen
    )
