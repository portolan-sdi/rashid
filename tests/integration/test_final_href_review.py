"""Final-review href regressions through the CLI and HTTPS transport."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json
from tests.integration.test_href_behavior import _check

pytestmark = pytest.mark.integration


def _publish(server: HttpsServer, root: Path) -> None:
    server.routes.update(
        {
            "/" + path.relative_to(root).as_posix(): Route(body=path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        }
    )


def test_live_probes_explicit_default_port(catalog: CatalogBuilder) -> None:
    # Loopback port 443 refuses the probe on unprivileged test hosts. The live
    # pass must report that refusal rather than skip the asset as off-host.
    catalog.collection(
        "roads",
        assets={"data": {"href": "https://127.0.0.1:443/a.bin", "roles": ["data"]}},
    )
    report = _check(catalog.write(), "--no-data", "--live", "--live-base-url", "https://127.0.0.1/")
    unavailable = [f for f in report["findings"] if f["rule_id"] == "PTL-LIV-000"]
    assert not any("no probeable asset hrefs" in f["message"] for f in unavailable)
    assert any("127.0.0.1" in f["message"] for f in unavailable)


def test_live_self_location_keeps_userinfo(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    catalog.collection("roads", assets={})
    root = catalog.write()
    claimed = https_server.url("catalog.json").replace("https://", "https://user:secret@")
    mutate_json(
        root / "catalog.json", lambda d: d["links"].append({"rel": "self", "href": claimed})
    )
    report = _check(root, "--no-data", "--live", "--live-base-url", https_server.base)
    assert any(f["rule_id"] == "PTL-LIV-007" for f in report["findings"])


def test_queried_document_asset_reads_its_own_bytes(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    payload = b"queried asset bytes"
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": "./collection.json?raw=1",
                "roles": ["data"],
                "type": "application/octet-stream",
                "file:size": len(payload),
                "file:checksum": "1220" + hashlib.sha256(payload).hexdigest(),
            }
        },
    )
    root = catalog.write()
    _publish(https_server, root)
    https_server.routes["/roads/collection.json?raw=1"] = Route(body=payload)
    report = _check(https_server.url("catalog.json"), "--no-live")
    assert not [f for f in report["findings"] if f["rule_id"] in ("PTL-DAT-001", "PTL-DAT-002")]
    assert any(s.path == "/roads/collection.json?raw=1" for s in https_server.seen)


def test_legal_path_delimiters_reach_the_same_https_resource(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    name = "a;b=,c:@!$&'()*+.bin?token=x"
    href = "./" + name
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": href,
                "roles": ["data"],
                "type": "application/octet-stream",
                "file:size": 3,
            }
        },
    )
    root = catalog.write()
    _publish(https_server, root)
    https_server.routes["/roads/" + name] = Route(body=b"abc")
    report = _check(https_server.url("catalog.json"), "--no-live")
    assert any(s.path == "/roads/" + name for s in https_server.seen)
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-DAT-002"]

    mutate_json(
        root / "roads/collection.json",
        lambda d: d["assets"]["data"].update(href=https_server.url("roads/" + name)),
    )
    https_server.routes["/roads/collection.json"] = Route(
        body=(root / "roads/collection.json").read_bytes()
    )
    https_server.seen.clear()
    absolute_report = _check(https_server.url("catalog.json"), "--no-live")
    assert any(s.path == "/roads/" + name for s in https_server.seen)
    assert not [f for f in absolute_report["findings"] if f["rule_id"] == "PTL-DAT-002"]
