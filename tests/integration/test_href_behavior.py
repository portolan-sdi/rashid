"""Href and organizing-catalog regressions through the real CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from rashid.cli import main
from tests.conftest import CatalogBuilder, nest_items_under_organizing_catalog

pytestmark = pytest.mark.integration

_COG = "image/tiff; application=geotiff; profile=cloud-optimized"


def _check(root: Path, *flags: str) -> dict[str, Any]:
    result = CliRunner().invoke(main, ["check", "--json", *flags, str(root)])
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
