"""Live and fetch regressions through the CLI."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json
from tests.integration.test_href_behavior import _check

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("read", ["crawl", "stream"])
@pytest.mark.parametrize(
    "escaped,literal", [("%3B", ";"), ("%5B", "["), ("%5D", "]"), ("%5E", "^"), ("%7C", "|")]
)
def test_fetch_fallback_keeps_object_identity(
    catalog: CatalogBuilder, https_server: HttpsServer, read: str, escaped: str, literal: str
) -> None:
    encoded_name, literal_name = f"a{escaped}v.json", f"a{literal}v.json"
    payload = b"the hosted asset"
    asset = {
        "href": f"./{encoded_name}",
        "type": "application/json",
        "roles": ["metadata"],
        "file:size": len(payload),
        "file:checksum": "1220" + hashlib.sha256(payload).hexdigest(),
    }
    col = catalog.collection("roads", assets={"metadata": asset} if read == "stream" else {})
    if read == "crawl":
        # Both routes serve recognizable STAC items; only the wrong one has file fields.
        col.item(
            "a",
            assets={
                "metadata": {
                    key: value for key, value in asset.items() if not key.startswith("file:")
                }
            },
        )
    root = catalog.write()
    wrong_payload = b"a different object"
    if read == "crawl":
        mutate_json(
            root / "roads/collection.json",
            lambda d: next(link for link in d["links"] if link["rel"] == "item").update(
                href=f"./{encoded_name}"
            ),
        )
        document = json.loads((root / "roads/a/a.json").read_bytes())
        for link in document["links"]:
            link["href"] = "../catalog.json" if link["rel"] == "root" else "./collection.json"
        payload = json.dumps(document).encode()
        document["assets"]["metadata"].update(
            {"file:size": 1, "file:checksum": "1220" + hashlib.sha256(b"other").hexdigest()}
        )
        wrong_payload = json.dumps(document).encode()
    for path in root.rglob("*"):
        if path.is_file():
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )
    # Separate routes prevent the test server from hiding a changed object.
    for length in (True, False):
        https_server.seen.clear()
        https_server.routes[f"/roads/{literal_name}"] = Route(body=payload, length=length)
        https_server.routes[f"/roads/{encoded_name}"] = Route(body=wrong_payload, length=length)
        report = _check(https_server.url("catalog.json"), "--no-live")
        assert report["files_checked"] == (3 if read == "crawl" else 2)
        findings = report["findings"]
        assert not [f for f in findings if f["rule_id"].startswith("PTL-DAT-")], findings
        assert not [f for f in findings if f["rule_id"] == "PTL-AST-003"], findings
        gets = [r.path for r in https_server.seen if r.method == "GET" and "v.json" in r.path]
        assert gets and set(gets) == {f"/roads/{literal_name}"}
