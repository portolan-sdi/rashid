"""Live and fetch regressions through the CLI."""

from __future__ import annotations

import pytest

from tests._https import HttpsServer, Route, _Handler
from tests.conftest import CatalogBuilder
from tests.integration.test_href_behavior import _check

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("length", [True, False], ids=["error-length", "no-length"])
def test_denied_probes_are_inaccessible(
    catalog: CatalogBuilder,
    https_server: HttpsServer,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    length: bool,
) -> None:
    def allow_preflight(handler: _Handler) -> None:
        handler.send_response(204)
        handler.send_header("Access-Control-Allow-Methods", "GET, HEAD")
        handler.send_header("Access-Control-Allow-Headers", "*")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    monkeypatch.setattr(_Handler, "do_OPTIONS", allow_preflight)
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": https_server.url("private.bin"),
                "type": "application/octet-stream",
                "roles": ["data"],
                "file:size": 1000,
            }
        },
    )
    https_server.routes["/private.bin"] = Route(status=status, body=b"denied", length=length)
    report = _check(catalog.write(), "--no-data", "--live")
    findings = [f for f in report["findings"] if f["rule_id"].startswith("PTL-LIV-")]
    assert findings and {f["rule_id"] for f in findings} == {"PTL-LIV-000"}, findings
    assert all(f["severity"] == "warning" and str(status) in f["message"] for f in findings)


def test_denied_get_still_checks_preflight(
    catalog: CatalogBuilder, https_server: HttpsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny_get(handler: _Handler) -> None:
        handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    monkeypatch.setattr(_Handler, "do_GET", deny_get)
    # HEAD succeeds, while OPTIONS omits the required preflight headers.
    https_server.routes["/data.bin"] = Route(body=b"x" * 1000)
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": https_server.url("data.bin"),
                "type": "application/octet-stream",
                "roles": ["data"],
                "file:size": 1000,
            }
        },
    )
    report = _check(catalog.write(), "--no-data", "--live")
    findings = [f for f in report["findings"] if f["rule_id"].startswith("PTL-LIV-")]
    assert {f["rule_id"] for f in findings} == {"PTL-LIV-000", "PTL-LIV-005"}, findings
    assert not report["passed"]
    assert next(f for f in findings if f["rule_id"] == "PTL-LIV-005")["severity"] == "error"


@pytest.mark.parametrize("status", [405, 404, 500])
@pytest.mark.parametrize("length", [False, True], ids=["no-length", "error-length"])
def test_non_denied_head_failure_keeps_length_error(
    catalog: CatalogBuilder,
    https_server: HttpsServer,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    length: bool,
) -> None:
    def reject_head(handler: _Handler) -> None:
        handler.send_response(status)
        if length:
            handler.send_header("Content-Length", "6")
        handler.send_header("Connection", "close")
        handler.end_headers()

    monkeypatch.setattr(_Handler, "do_HEAD", reject_head)
    https_server.routes["/data.bin"] = Route(
        body=b"x" * 1000,
        headers={
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "*",
            "Access-Control-Allow-Methods": "GET, HEAD",
            "Access-Control-Allow-Headers": "*",
        },
    )
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": https_server.url("data.bin"),
                "type": "application/octet-stream",
                "roles": ["data"],
                "file:size": 1000,
            }
        },
    )
    report = _check(catalog.write(), "--no-data", "--live")
    findings = [f for f in report["findings"] if f["rule_id"].startswith("PTL-LIV-")]
    assert not report["passed"]
    assert len(findings) == 1 and findings[0]["rule_id"] == "PTL-LIV-002", findings
    assert findings[0]["severity"] == "error"
    assert {request.method for request in https_server.seen} == {"GET", "OPTIONS"}
