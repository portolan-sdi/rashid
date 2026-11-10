"""Catalog construction and CLI assertions for the owner regressions."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from rashid.cli import main
from tests.conftest import CatalogBuilder


def _catalog(root: Path, **fields: object) -> Path:
    cat = CatalogBuilder(root)
    cat.collection("layers", assets={}, **fields)
    cat.write()
    return root / "layers"


def _declare(directory: Path, names: list[str]) -> None:
    path = directory / "collection.json"
    data = json.loads(path.read_text())
    data["assets"] = {
        name: {
            "href": "./" + name,
            "type": "image/tiff" if name.endswith(".tif") else "application/vnd.apache.parquet",
            "roles": ["data"],
        }
        for name in names
    }
    path.write_text(json.dumps(data))


def _findings(root: Path, rule: str) -> list[dict]:
    result = CliRunner().invoke(main, ["check", "--json", "--data-scope", "local", str(root)])
    assert result.exit_code in (0, 1), result.output
    findings = json.loads(result.output)["findings"]
    assert not any(f["rule_id"] == "PTL-DAT-000" for f in findings), findings
    return [f for f in findings if f["rule_id"] == rule]
