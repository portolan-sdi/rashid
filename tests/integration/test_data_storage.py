"""Cloud-native storage rules: COG validity/statistics and GeoParquet layout.

Needs the ``reis[data]`` extra; skips without it. Drives the check functions
directly on generated assets — a spec-compliant asset produces no findings, and
each non-compliant variant raises exactly the rule it violates (formats.md:30/39/
50/91/95).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")

import reis.data.checks as checks  # noqa: E402
from reis.catalog import Node  # noqa: E402
from reis.data import (  # noqa: E402
    DAT_COG,
    DAT_COG_STATS,
    DAT_GEOPARQUET_VERSION,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
)
from reis.data.reader import Locator  # noqa: E402
from reis.model import Severity  # noqa: E402
from tests.integration import _data_assets as assets  # noqa: E402

pytestmark = pytest.mark.integration


def _loc(path: Path) -> Locator:
    return Locator(is_remote=False, source=str(path))


class _FileReader:
    """An asset reader backed by one local file, for driving check_node."""

    def __init__(self, href: str, path: Path) -> None:
        self._href = href
        self._path = path

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        return iter([self._path.read_bytes()]) if href == self._href else None

    def locate(self, node: Node, href: str) -> Locator | None:
        return _loc(self._path) if href == self._href else None


def _node_with_asset(asset: dict) -> Node:
    return Node(
        path=PurePosixPath("layers/scene/scene.json"),
        abs_path=Path("/nowhere/scene.json"),
        kind="item",
        id="scene",
        data={"type": "Feature", "bbox": [4.0, 50.0, 6.0, 52.0], "assets": {"a": asset}},
    )


def _gpq(path: Path) -> list:
    return checks._check_geoparquet("data", _loc(path))


def _raster(path: Path) -> list:
    return checks._check_raster("data", _loc(path))


# --- GeoParquet ------------------------------------------------------------


def test_compliant_geoparquet_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "ok.parquet"
    assets.write_geoparquet(path)
    assert _gpq(path) == []


def test_unordered_rows_flag_dat_006(tmp_path: Path) -> None:
    path = tmp_path / "unordered.parquet"
    assets.write_geoparquet(path, points=assets.interleaved_points())
    defects = _gpq(path)
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.ERROR


def test_missing_rowgroup_stats_flag_dat_007(tmp_path: Path) -> None:
    path = tmp_path / "no_covering.parquet"
    assets.write_geoparquet(path, covering=False)
    defects = _gpq(path)
    assert [d.rule_id for d in defects] == [DAT_ROWGROUP_STATS]
    assert defects[0].severity is Severity.ERROR


# --- native GeospatialStatistics (GeoParquet 2.x / Parquet GEOMETRY) ---------
#
# No dependency in the [data] extra can WRITE native GeospatialStatistics yet
# (pyarrow reads them from 21.0), so these drive the reader over duck-typed
# row-group metadata shaped exactly like pyarrow's.


class _FakeGeoStats:
    def __init__(self, box: tuple[float, float, float, float]) -> None:
        self.xmin, self.ymin, self.xmax, self.ymax = box


class _FakeColumn:
    def __init__(self, path: str, geo: _FakeGeoStats | None) -> None:
        self.path_in_schema = path
        self.geo_statistics = geo


class _FakeRowGroup:
    def __init__(self, columns: list[_FakeColumn]) -> None:
        self._columns = columns
        self.num_columns = len(columns)
        self.num_rows = 1

    def column(self, j: int) -> _FakeColumn:
        return self._columns[j]


class _FakeParquet:
    """Duck-types what _row_group_bboxes touches: itself its own .metadata."""

    def __init__(self, groups: list[_FakeRowGroup]) -> None:
        self.metadata = self
        self._groups = groups
        self.num_row_groups = len(groups)

    def row_group(self, i: int) -> _FakeRowGroup:
        return self._groups[i]


_GEO_2X = {"version": "2.0.0", "primary_column": "geometry", "columns": {"geometry": {}}}


def test_native_geo_statistics_satisfy_dat_007() -> None:
    ordered = [(0.0, 0.0, 1.0, 1.0), (1.0, 1.0, 2.0, 2.0), (2.0, 2.0, 3.0, 3.0)]
    groups = [_FakeRowGroup([_FakeColumn("geometry", _FakeGeoStats(b))]) for b in ordered]
    boxes = checks._row_group_bboxes(_FakeParquet(groups), _GEO_2X)
    assert boxes == ordered


def test_absent_native_statistics_still_return_none() -> None:
    groups = [_FakeRowGroup([_FakeColumn("geometry", None)])]
    assert checks._row_group_bboxes(_FakeParquet(groups), _GEO_2X) is None


def test_oversized_rowgroup_flags_dat_008(tmp_path: Path) -> None:
    path = tmp_path / "big.parquet"
    assets.write_geoparquet(path, points=assets.ordered_points(150_001), row_group_size=200_000)
    defects = _gpq(path)
    assert DAT_ROWGROUP_SIZE in [d.rule_id for d in defects]
    assert next(d for d in defects if d.rule_id == DAT_ROWGROUP_SIZE).severity is Severity.ERROR


def test_plain_parquet_is_skipped(tmp_path: Path) -> None:
    # No 'geo' metadata key: legitimate tabular Parquet, not GeoParquet. The
    # storage rules must not fire (media type alone cannot tell them apart).
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False, points=assets.interleaved_points())
    assert _gpq(path) == []


# --- COG -------------------------------------------------------------------


def test_compliant_cog_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "cog.tif"
    assets.write_cog(path)
    assert _raster(path) == []


def test_missing_band_stats_flags_dat_009(tmp_path: Path) -> None:
    path = tmp_path / "no_stats.tif"
    assets.write_cog(path, stats=False)
    defects = _raster(path)
    assert {d.rule_id for d in defects} == {DAT_COG_STATS, DAT_VALID_PERCENT}
    dat_009 = next(d for d in defects if d.rule_id == DAT_COG_STATS)
    assert dat_009.severity is Severity.ERROR


def test_missing_valid_percent_is_a_warning(tmp_path: Path) -> None:
    # formats.md: valid percent is a SHOULD on a band without a nodata value.
    path = tmp_path / "no_vp.tif"
    assets.write_cog(path, valid_percent=False)
    defects = _raster(path)
    assert [d.rule_id for d in defects] == [DAT_VALID_PERCENT]
    assert defects[0].severity is Severity.WARNING


def test_missing_valid_percent_with_nodata_is_an_error(tmp_path: Path) -> None:
    # formats.md: valid percent is a MUST when the band has a nodata value.
    path = tmp_path / "no_vp_nodata.tif"
    assets.write_cog(path, valid_percent=False, nodata=0)
    defects = _raster(path)
    assert [d.rule_id for d in defects] == [DAT_VALID_PERCENT]
    assert defects[0].severity is Severity.ERROR


def test_valid_percent_with_nodata_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "vp_nodata.tif"
    assets.write_cog(path, nodata=0)
    assert _raster(path) == []


def test_missing_overviews_flag_dat_011(tmp_path: Path) -> None:
    # cog_validate accepts an overview-less COG with only a warning, so the
    # structural check alone (PTL-DAT-004) never catches this.
    path = tmp_path / "no_ovr.tif"
    assets.write_cog(path, overviews=False)
    defects = _raster(path)
    assert [d.rule_id for d in defects] == [DAT_OVERVIEWS]
    assert defects[0].severity is Severity.ERROR


def test_single_tile_raster_needs_no_overviews(tmp_path: Path) -> None:
    # A raster no larger than one 512px tile renders from full resolution.
    path = tmp_path / "small.tif"
    assets.write_cog(path, overviews=False, size=256)
    assert _raster(path) == []


def test_non_cog_raster_flags_dat_004(tmp_path: Path) -> None:
    path = tmp_path / "striped.tif"
    assets.write_plain_tiff(path)
    ids = [d.rule_id for d in _raster(path)]
    assert DAT_COG in ids
    assert next(d for d in _raster(path) if d.rule_id == DAT_COG).severity is Severity.ERROR


# --- alternate / source exemption ------------------------------------------


def test_source_alternate_tiff_is_exempt(tmp_path: Path) -> None:
    # A non-cloud-native original kept alongside the primary (roles data+source)
    # is exempt from the COG MUST — the reference catalog does exactly this.
    path = tmp_path / "source.tif"
    assets.write_plain_tiff(path)
    asset = {
        "href": "./source.tif",
        "type": "image/tiff; application=geotiff",
        "roles": ["data", "source"],
    }
    reader = _FileReader("./source.tif", path)
    assert checks.check_node(_node_with_asset(asset), reader) == []


def test_primary_non_cog_tiff_is_still_flagged(tmp_path: Path) -> None:
    path = tmp_path / "primary.tif"
    assets.write_plain_tiff(path)
    asset = {
        "href": "./primary.tif",
        "type": "image/tiff; application=geotiff",
        "roles": ["data"],
    }
    reader = _FileReader("./primary.tif", path)
    ids = [d.rule_id for d in checks.check_node(_node_with_asset(asset), reader)]
    assert DAT_COG in ids


# --- GeoParquet version (PTL-DAT-012) ---------------------------------------


def test_geoparquet_1_0_flags_dat_012(tmp_path: Path) -> None:
    path = tmp_path / "legacy.parquet"
    assets.write_geoparquet(path, version="1.0.0", covering=False)
    defects = _gpq(path)
    ids = [d.rule_id for d in defects]
    assert DAT_GEOPARQUET_VERSION in ids
    flagged = next(d for d in defects if d.rule_id == DAT_GEOPARQUET_VERSION)
    assert flagged.severity is Severity.ERROR
    assert "1.0.0" in flagged.message


def test_geoparquet_2_x_version_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "v2.parquet"
    assets.write_geoparquet(path, version="2.0.0")
    assert DAT_GEOPARQUET_VERSION not in [d.rule_id for d in _gpq(path)]


def test_missing_geo_version_flags_dat_012(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.parquet"
    assets.write_geoparquet(path, version="")
    ids = [d.rule_id for d in _gpq(path)]
    assert DAT_GEOPARQUET_VERSION in ids


# --- internal tile size (PTL-DAT-013) ---------------------------------------


def _tiles(path: Path) -> list:
    return checks._check_tile_size("data", _loc(path))


def test_default_512_tiles_are_clean(tmp_path: Path) -> None:
    path = tmp_path / "ok.tif"
    assets.write_cog(path)
    assert _tiles(path) == []


def test_oversized_tiles_flag_dat_013(tmp_path: Path) -> None:
    path = tmp_path / "big_tiles.tif"
    assets.write_cog(path, size=2048, blocksize=1024)
    defects = [d for d in _raster(path) if d.rule_id == DAT_TILE_SIZE]
    assert len(defects) == 1
    assert defects[0].severity is Severity.ERROR
    assert "1024x1024" in defects[0].message


def test_non_square_tiles_flag_dat_013(tmp_path: Path) -> None:
    path = tmp_path / "oblong_tiles.tif"
    assets.write_tiled_tiff(path, blockx=512, blocky=256)
    defects = _tiles(path)
    assert [d.rule_id for d in defects] == [DAT_TILE_SIZE]
    assert "square" in defects[0].message


def test_untiled_raster_is_skipped_by_the_tile_check(tmp_path: Path) -> None:
    # Tiling itself is a base-COG requirement; cog_validate owns reporting it.
    path = tmp_path / "striped.tif"
    assets.write_plain_tiff(path)
    assert _tiles(path) == []


# --- overview cutoff uses the file's own tile size (PTL-DAT-011) -------------


def test_raster_larger_than_its_own_small_tile_needs_overviews(tmp_path: Path) -> None:
    # 400px with 256px tiles: within the old fixed 512px cutoff, but larger
    # than its own tile, so overviews are required (formats.md:133).
    path = tmp_path / "small_tiles.tif"
    assets.write_cog(path, size=400, blocksize=256, overviews=False)
    defects = [d for d in _raster(path) if d.rule_id == DAT_OVERVIEWS]
    assert len(defects) == 1
    assert "256px" in defects[0].message


# --- partition schema consistency (PTL-DAT-014) ------------------------------


def _partitioned_collection(directory: Path, glob: str = "parts/*.parquet") -> Node:
    return Node(
        path=PurePosixPath("buildings/collection.json"),
        abs_path=directory / "collection.json",
        kind="collection",
        id="buildings",
        data={"type": "Collection", "partition:glob": glob},
    )


def test_consistent_partition_schemas_are_clean(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet")
    assert checks._check_partition_schemas(_partitioned_collection(tmp_path)) == []


def test_diverging_partition_schemas_flag_dat_014(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet", columns={"extra": list(range(6))})
    defects = checks._check_partition_schemas(_partitioned_collection(tmp_path))
    assert [d.rule_id for d in defects] == [DAT_PARTITION_SCHEMA]
    assert defects[0].severity is Severity.ERROR
    assert "extra" in defects[0].message
    assert defects[0].json_pointer == "/partition:glob"


def test_remote_partition_glob_is_skipped(tmp_path: Path) -> None:
    node = _partitioned_collection(tmp_path, glob="s3://bucket/parts/*.parquet")
    assert checks._check_partition_schemas(node) == []


def test_escaping_partition_glob_is_skipped(tmp_path: Path) -> None:
    node = _partitioned_collection(tmp_path, glob="../elsewhere/*.parquet")
    assert checks._check_partition_schemas(node) == []


def test_single_partition_file_has_nothing_to_compare(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assert checks._check_partition_schemas(_partitioned_collection(tmp_path)) == []
