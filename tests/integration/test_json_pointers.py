"""JSON Pointer locations in CLI reports resolve to the original keys."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from rashid.cli import main
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "rule, field", [("PTL-AST-003", "file:checksum"), ("PTL-DAT-002", "file:size")]
)
def test_cli_asset_key_pointer(catalog: CatalogBuilder, rule: str, field: str) -> None:
    catalog.collection(
        "roads",
        assets={
            "a/b~c": {
                "href": "./data.bin",
                "type": "application/octet-stream",
                "roles": ["data"],
                "file:size": 1,
            }
        },
    )
    root = catalog.write()
    (root / "roads/data.bin").write_bytes(b"abc")
    result = CliRunner().invoke(main, ["check", "--json", str(root)], catch_exceptions=False)
    assert result.exit_code == 1
    findings = json.loads(result.stdout)["findings"]
    (finding,) = [f for f in findings if f["rule_id"] == rule]
    assert finding["json_pointer"] == f"/assets/a~1b~0c/{field}"


@pytest.mark.parametrize("key, escaped", [("a/b~c", "a~1b~0c"), ("~1", "~01")])
def test_cli_property_key_pointer(
    catalog: CatalogBuilder, https_server: HttpsServer, key: str, escaped: str
) -> None:
    uri = https_server.url("property.json")
    https_server.routes["/property.json"] = Route(
        body=json.dumps(
            {"properties": {"properties": {"properties": {key: {"type": "integer"}}}}}
        ).encode()
    )
    collection = catalog.collection("roads")
    collection.item(
        "one", properties={"datetime": "2024-01-01T00:00:00Z", key: "wrong"}, stac_extensions=[uri]
    )
    result = CliRunner().invoke(
        main,
        ["check", "--json", "--no-data", "--schema-allow-network", str(catalog.write())],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    (finding,) = [f for f in json.loads(result.stdout)["findings"] if f["rule_id"] == "PTL-EXT-001"]
    assert finding["json_pointer"] == "/properties/" + escaped


def test_cli_schema_error_path_pointer(catalog: CatalogBuilder, https_server: HttpsServer) -> None:
    uri = https_server.url("nested.json")
    https_server.routes["/nested.json"] = Route(
        body=json.dumps(
            {"properties": {"a/b~c": {"items": {"properties": {"a/b~c": {"type": "integer"}}}}}}
        ).encode()
    )
    catalog.overrides.update(stac_extensions=[uri], **{"a/b~c": [{"a/b~c": "wrong"}]})
    result = CliRunner().invoke(
        main,
        ["check", "--json", "--no-data", "--schema-allow-network", str(catalog.write())],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    (finding,) = [f for f in json.loads(result.stdout)["findings"] if f["rule_id"] == "PTL-EXT-001"]
    assert finding["json_pointer"] == "/a~1b~0c/0/a~1b~0c"
