"""Public CLI regressions for the GeoParquet and file owner track (#266)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._https import HttpsServer
from tests.conftest import CatalogBuilder
from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare, _findings

pytestmark = pytest.mark.integration


def test_collection_extent_contains_partial_assets(tmp_path: Path) -> None:
    """#248: a collection extent contains each asset, and still detects overflow."""
    directory = _catalog(tmp_path)
    paths = [directory / name for name in ("west.parquet", "east.parquet")]
    for path, points in zip(
        paths, [[(4.0, 50.0), (5.0, 51.0)], [(5.0, 51.0), (6.0, 52.0)]], strict=True
    ):
        assets.write_geoparquet(path, points=points)
    _declare(directory, [p.name for p in paths])
    assert not _findings(tmp_path, "PTL-DAT-005")
    assets.write_geoparquet(paths[1], points=[(5.0, 51.0), (7.0, 53.0)])
    findings = _findings(tmp_path, "PTL-DAT-005")
    assert len(findings) == 1 and findings[0]["json_pointer"] == "/extent/spatial/bbox/0"

    cat = CatalogBuilder(tmp_path / "items")
    cat.collection("layers", assets={}).item(
        "part",
        assets={"data": {"href": "./part.parquet", "type": "application/vnd.apache.parquet"}},
    )
    cat.write()
    assets.write_geoparquet(
        tmp_path / "items/layers/part/part.parquet", points=[(4.0, 50.0), (5.0, 51.0)]
    )
    findings = _findings(tmp_path / "items", "PTL-DAT-005")
    assert len(findings) == 1 and "does not match" in findings[0]["message"]


def test_collection_extent_can_exclude_a_raster_nodata_collar(tmp_path: Path) -> None:
    """A grid is an outer bound, not the footprint of the raster's valid pixels."""
    import json

    import rasterio

    valid_bbox = [4.8, 50.9, 5.1, 51.2]
    directory = _catalog(tmp_path, extent={"spatial": {"bbox": [valid_bbox]}})
    path = directory / "collar.tif"
    assets.write_cog(path, size=200, blocksize=256, nodata=0)
    with rasterio.open(path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        pixels = dst.read()
        pixels.fill(0)
        pixels[:, 80:110, 80:110] = 1
        dst.write(pixels)
    _declare(directory, [path.name])
    assert not _findings(tmp_path, "PTL-DAT-005")
    metadata = directory / "collection.json"
    document = json.loads(metadata.read_text())
    document["extent"]["spatial"]["bbox"] = [[-10, -10, -9, -9]]
    metadata.write_text(json.dumps(document))
    assert len(_findings(tmp_path, "PTL-DAT-005")) == 1


@pytest.mark.parametrize(
    ("boxes", "extent"),
    [([[4, 50, 5, 51]], [0, 0, 40, 60]), ([[4, 50, 5, 51], [5, 51, 6, 52]], [3, 49, 9, 55])],
)
def test_collection_extent_matches_the_asset_union(
    tmp_path: Path, boxes: list[list[float]], extent: list[float]
) -> None:
    import json

    directory = _catalog(tmp_path, extent={"spatial": {"bbox": [extent]}})
    names = []
    for index, box in enumerate(boxes):
        name = f"part-{index}.parquet"
        names.append(name)
        assets.write_geoparquet(directory / name, points=[(box[0], box[1]), (box[2], box[3])])
    _declare(directory, names)
    findings = _findings(tmp_path, "PTL-DAT-005")
    assert len(findings) == 1 and findings[0]["severity"] == "warning"
    metadata = directory / "collection.json"
    document = json.loads(metadata.read_text())
    document["extent"]["spatial"]["bbox"] = [[4.005, 50, boxes[-1][2], boxes[-1][3]]]
    metadata.write_text(json.dumps(document))
    assert not _findings(tmp_path, "PTL-DAT-005")


def test_collection_extent_with_skipped_remote_asset(
    tmp_path: Path, https_server: HttpsServer
) -> None:
    """Local scope cannot conclude that an incomplete union is too small."""
    import json

    from click.testing import CliRunner

    from rashid.cli import main
    from tests._https import Route

    directory = _catalog(tmp_path, extent={"spatial": {"bbox": [[0, 0, 2, 1]]}})
    assets.write_geoparquet(directory / "west.parquet", points=[(0, 0), (1, 1)])
    assets.write_geoparquet(directory / "east.parquet", points=[(1, 0), (2, 1)])
    _declare(directory, ["west.parquet", "east.parquet"])
    path = directory / "collection.json"
    document = json.loads(path.read_text())
    document["assets"]["east.parquet"]["href"] = https_server.url("east.parquet")
    path.write_text(json.dumps(document))
    https_server.routes["/east.parquet"] = Route(body=(directory / "east.parquet").read_bytes())

    for extent in ([0, 0, 2, 1], [0, 0, 0.5, 1], [0, 0, 3, 1]):
        document["extent"]["spatial"]["bbox"] = [extent]
        path.write_text(json.dumps(document))
        for scope in ("local", "all"):
            result = CliRunner().invoke(
                main, ["check", "--json", "--data-scope", scope, str(tmp_path)]
            )
            assert result.exit_code in (0, 1), result.output
            findings = [
                finding
                for finding in json.loads(result.output)["findings"]
                if finding["rule_id"] == "PTL-DAT-005"
            ]
            expected = extent[2] < 1 or (scope == "all" and extent[2] > 2)
            assert len(findings) == int(expected), (scope, extent, findings)


@pytest.mark.parametrize("scope", ["all", "local"])
def test_collection_extent_ignores_readable_nonspatial_parquet(tmp_path: Path, scope: str) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from tests.integration.test_href_behavior import _check

    directory = _catalog(tmp_path, extent={"spatial": {"bbox": [[0, 0, 3, 1]]}})
    assets.write_geoparquet(directory / "spatial.parquet", points=[(0, 0), (1, 1)])
    pq.write_table(pa.table({"value": [1, 2]}), directory / "table.parquet")
    _declare(directory, ["spatial.parquet", "table.parquet"])
    report = _check(tmp_path, "--data-scope", scope)
    findings = [f for f in report["findings"] if f["rule_id"] == "PTL-DAT-005"]
    assert len(findings) == 1
    assert findings[0]["json_pointer"] == "/extent/spatial/bbox/0"
    assert "asset union" in findings[0]["message"]


@pytest.mark.parametrize("scope", ["all", "local"])
def test_collection_extent_with_geoparquet_without_bbox(tmp_path: Path, scope: str) -> None:
    import json

    import pyarrow.parquet as pq

    from tests.conftest import mutate_json
    from tests.integration.test_href_behavior import _check

    directory = _catalog(tmp_path, extent={"spatial": {"bbox": [[0, 0, 2, 1]]}})
    assets.write_geoparquet(directory / "west.parquet", points=[(0, 0), (1, 1)])
    east = directory / "east.parquet"
    assets.write_geoparquet(east, points=[(1, 0), (2, 1)])
    table = pq.read_table(east)
    metadata = dict(table.schema.metadata)
    geo = json.loads(metadata[b"geo"])
    del geo["columns"][geo["primary_column"]]["bbox"]
    metadata[b"geo"] = json.dumps(geo).encode()
    pq.write_table(table.replace_schema_metadata(metadata), east)
    _declare(directory, ["west.parquet", "east.parquet"])

    for extent, expected in (([0, 0, 2, 1], 0), ([0, 0, 0.5, 1], 1)):
        mutate_json(
            directory / "collection.json",
            lambda d, extent=extent: d["extent"]["spatial"].update(bbox=[extent]),
        )
        report = _check(tmp_path, "--data-scope", scope)
        findings = [f for f in report["findings"] if f["rule_id"] == "PTL-DAT-005"]
        assert len(findings) == expected, (scope, extent, findings)
        if findings:
            assert findings[0]["json_pointer"] == "/extent/spatial/bbox/0"
