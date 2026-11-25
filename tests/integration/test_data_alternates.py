"""The data pass reads a non-https href through its https alternate (PORTO-CORE-087)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from rashid_core.catalog import CatalogGraph
from rashid_core.cli import main
from rashid_core.data import _reads_remote
from rashid_core.data.reader import FilesystemHttpReader
from tests._https import HttpsServer, Route
from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare

pytestmark = pytest.mark.integration


def _s3_catalog(tmp_path: Path, alternate: str | None, **asset: Any) -> Path:
    directory = _catalog(tmp_path)
    assets.write_geoparquet(directory / "roads.parquet")
    _declare(directory, ["roads.parquet"])
    path = directory / "collection.json"
    document = json.loads(path.read_text())
    entry = document["assets"]["roads.parquet"]
    entry["href"] = "s3://bucket/roads.parquet"
    if alternate is not None:
        entry["alternate"] = {"https": {"href": alternate}}
    entry.update(asset)
    path.write_text(json.dumps(document))
    return directory


def _check(root: Path) -> list[dict[str, Any]]:
    result = CliRunner().invoke(main, ["check", "--json", str(root)])
    assert result.exit_code in (0, 1), result.output
    findings: list[dict[str, Any]] = json.loads(result.output)["findings"]
    return [f for f in findings if f["rule_id"].startswith("PTL-DAT-")]


def test_an_s3_href_is_read_through_its_https_alternate(
    tmp_path: Path, https_server: HttpsServer
) -> None:
    directory = _s3_catalog(tmp_path, https_server.url("roads.parquet"), **{"file:size": 1})
    body = (directory / "roads.parquet").read_bytes()
    https_server.routes["/roads.parquet"] = Route(body=body)
    findings = _check(tmp_path)
    sizes = [f for f in findings if f["rule_id"] == "PTL-DAT-002"]
    assert len(sizes) == 1, findings
    assert str(len(body)) in sizes[0]["message"], findings
    assert not any(f["rule_id"] == "PTL-DAT-000" for f in findings)
    assert {r.path for r in https_server.seen} == {"/roads.parquet"}
    graph = CatalogGraph.load(tmp_path)
    node = next(n for n in graph.iter("collection") if n.id == "layers")
    assert _reads_remote(node, FilesystemHttpReader(graph))


def test_an_s3_href_without_an_alternate_is_listed_once(tmp_path: Path) -> None:
    columns = [{"name": "value", "type": "int64"}]
    _s3_catalog(tmp_path, None, **{"table:columns": columns})
    findings = _check(tmp_path)
    notes = [f for f in findings if f["rule_id"] == "PTL-DAT-000"]
    assert len(notes) == 1, findings
    assert notes[0]["severity"] == "info", findings
    assert "layers/collection.json#/assets/roads.parquet" in notes[0]["message"]
    # The column check reads no bytes, so it still runs on the unreachable asset.
    assert [f["rule_id"] for f in findings if f["severity"] == "warning"] == ["PTL-DAT-020"]
