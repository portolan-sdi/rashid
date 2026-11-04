"""CLI and API regressions from the T1 review."""

from __future__ import annotations

from urllib.parse import quote

import pytest

from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json
from tests.integration.test_href_behavior import _check

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("location", ["self", "asset"])
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("data_flag", ["--no-data", "--data"])
def test_malformed_authority_is_a_finding_not_a_crash(
    catalog: CatalogBuilder, location: str, data_flag: str, live: bool, https_server: HttpsServer
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    if location == "self":
        mutate_json(
            root / "catalog.json",
            lambda d: d["links"].append({"rel": "self", "href": "https://[broken/catalog.json"}),
        )
        expected_path, expected_pointer = "catalog.json", "/links/4/href"
    else:
        mutate_json(
            root / "roads/collection.json",
            lambda d: d["assets"]["data"].update(href="https://[broken/data.parquet"),
        )
        expected_path, expected_pointer = "roads/collection.json", "/assets/data/href"
    live_flags = ("--live", "--live-base-url", https_server.base) if live else ("--no-live",)
    report = _check(root, data_flag, *live_flags)
    syntax = [f for f in report["findings"] if f["rule_id"] == "PTL-LNK-006"]
    assert len(syntax) == 1
    assert syntax[0]["path"] == expected_path
    assert syntax[0]["json_pointer"] == expected_pointer
    assert "valid URI reference" in syntax[0]["message"]
    assert syntax[0]["severity"] == "error"


@pytest.mark.parametrize("directory", ["my roads", "my %20 roads", "東京"])
def test_encoded_structural_target_resolves_to_the_local_filename(
    catalog: CatalogBuilder, directory: str, https_server: HttpsServer
) -> None:
    catalog.collection(directory)
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: next(link for link in d["links"] if link.get("rel") == "child").update(
            href="./" + quote(directory, safe="") + "/collection.json"
        ),
    )
    report = _check(root, "--no-data")
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-LNK-006"]

    for path in root.rglob("*"):
        if path.is_file():
            uri_path = "/" + quote(path.relative_to(root).as_posix(), safe="/")
            https_server.routes[uri_path] = Route(body=path.read_bytes())
    remote = _check(https_server.url("catalog.json"), "--no-data", "--no-live")
    assert remote["files_checked"] == 2
    assert not [f for f in remote["findings"] if f["rule_id"] == "PTL-LNK-006"]
    assert all("my roads/" not in f["path"] for f in remote["findings"] if directory == "my roads")


@pytest.mark.parametrize("scope", ["all", "local"])
def test_encoded_asset_href_reads_the_local_filename(catalog: CatalogBuilder, scope: str) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": "./my%20file.parquet",
                "roles": ["data"],
                "type": "application/octet-stream",
                "file:size": 2,
            }
        },
    )
    root = catalog.write()
    (root / "roads/my file.parquet").write_bytes(b"abc")
    findings = _check(root, "--data-scope", scope)["findings"]
    assert [f for f in findings if f["rule_id"] == "PTL-DAT-002"]

    # Encoded delimiters are decoded before containment, not after opening.
    outside = root.parent / "secret.bin"
    outside.write_bytes(b"abc")
    for href in ("%2e%2e/%2e%2e/secret.bin", "%2Fsecret.bin", "..%5Csecret.bin", "a%00b"):
        mutate_json(
            root / "roads/collection.json",
            lambda d, href=href: d["assets"]["data"].update(href=href),
        )
        findings = _check(root, "--data-scope", scope)["findings"]
        assert not [f for f in findings if f["rule_id"] == "PTL-DAT-002"]


@pytest.mark.parametrize("href", ["./%63ollection.json", "./%63ollection.json?token=x#piece"])
def test_encoded_local_self_reference_keeps_the_file_field_exemption(
    catalog: CatalogBuilder, href: str
) -> None:
    catalog.collection(
        "roads",
        assets={
            "metadata": {
                "href": href,
                "type": "application/json",
                "roles": ["metadata"],
            }
        },
    )
    report = _check(catalog.write(), "--no-data")
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-AST-003"]


@pytest.mark.parametrize(
    "href,self_reference",
    [("%2e/../a.json", False), ("%2e/../a/a.json", True)],
)
def test_encoded_dot_self_identity_matches_local_file_access(
    catalog: CatalogBuilder, href: str, self_reference: bool
) -> None:
    catalog.collection("roads", assets={}).item(
        "a",
        assets={
            "metadata": {
                "href": href,
                "type": "application/json",
                "roles": ["metadata"],
                "file:size": 1,
            }
        },
    )
    root = catalog.write()
    (root / "roads/a.json").write_bytes(b"{}")
    findings = _check(root, "--data")["findings"]
    self_errors = [
        f for f in findings if f["rule_id"] == "PTL-AST-003" and f["severity"] == "error"
    ]
    assert len(self_errors) == int(self_reference)
    if self_reference:
        assert self_errors[0]["json_pointer"] == "/assets/metadata/file:size"
    # A non-self target is actually read; genuine self-references skip byte checks.
    assert any(f["rule_id"] == "PTL-DAT-002" for f in findings) is not self_reference
