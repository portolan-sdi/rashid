"""Predicate regressions through the real JSON CLI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from rashid.cli import main
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.integration

_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


@pytest.mark.parametrize(
    "roles", [["visual", "overview", "cloud-optimized"], ["visual", "overview"]]
)
def test_browse_cog_needs_no_style(catalog: CatalogBuilder, roles: list[str]) -> None:
    collection = catalog.collection(
        "browse",
        assets={"browse": {"href": "./browse.tif", "type": _COG_TYPE, "roles": roles}},
    )
    result = CliRunner().invoke(main, ["check", "--no-data", "--json", str(catalog.write())])
    report = json.loads(result.output)
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-VIZ-002"]

    # A browse image cannot exempt a second render path that still needs a style.
    collection.overrides["assets"]["tiles"] = {
        "href": "./browse.pmtiles",
        "type": "application/vnd.pmtiles",
        "roles": ["visual"],
    }
    result = CliRunner().invoke(main, ["check", "--no-data", "--json", str(catalog.write())])
    report = json.loads(result.output)
    assert len([f for f in report["findings"] if f["rule_id"] == "PTL-VIZ-002"]) == 1


@pytest.mark.parametrize("roles", [["visual", "data"], ["visual", "overview", "data"]])
def test_visual_data_cog_still_needs_style(catalog: CatalogBuilder, roles: list[str]) -> None:
    catalog.collection(
        "raster",
        assets={"data": {"href": "./data.tif", "type": _COG_TYPE, "roles": roles}},
    )
    result = CliRunner().invoke(main, ["check", "--no-data", "--json", str(catalog.write())])
    report = json.loads(result.output)
    assert len([f for f in report["findings"] if f["rule_id"] == "PTL-VIZ-002"]) == 1
