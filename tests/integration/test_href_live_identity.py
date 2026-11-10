"""Encoded identities through the public API and local HTTPS transport."""

from __future__ import annotations

import json
import sys

import pytest

from rashid import validate
from rashid.api import ValidationOptions
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json

pytestmark = pytest.mark.integration


def test_encoded_slash_document_cannot_overwrite_the_plain_document(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    catalog.collection("a", license="not-a-license")
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {
                "rel": "child",
                "href": "./a%2Fcollection.json",
                "type": "application/json",
                "title": "Other",
            }
        ),
    )
    for path in root.rglob("*"):
        if path.is_file():
            https_server.routes["/" + path.relative_to(root).as_posix()] = Route(
                body=path.read_bytes()
            )
    other = json.loads((root / "a/collection.json").read_text())
    other.update(id="other", license="CC-BY-4.0")
    https_server.routes["/a%2Fcollection.json"] = Route(body=json.dumps(other).encode())
    report = validate(
        https_server.url("catalog.json"), options=ValidationOptions(data=False, live=False)
    )
    assert any(f.rule_id == "PTL-LIC-001" and f.object_id == "a" for f in report.findings)
    assert any(
        "encoded" in f.message and "a%2Fcollection.json" in f.message for f in report.findings
    )
    assert any(
        f.rule_id == "PTL-LNK-006" and f.actual == "./a%2Fcollection.json" for f in report.findings
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows filenames cannot contain a colon")
@pytest.mark.parametrize("scope", ["all", "local"])
def test_encoded_colon_is_relative_at_local_file_access(
    catalog: CatalogBuilder, scope: str
) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": {
                "href": "data%3A2024.parquet",
                "type": "application/octet-stream",
                "roles": ["data"],
                "file:size": 2,
            }
        },
    )
    root = catalog.write()
    (root / "roads/data:2024.parquet").write_bytes(b"abc")
    # Use the public CLI for its all/local data-reader selection.
    from tests.integration.test_href_behavior import _check

    findings = _check(root, "--data-scope", scope)["findings"]
    assert any(f["rule_id"] == "PTL-DAT-002" for f in findings)
