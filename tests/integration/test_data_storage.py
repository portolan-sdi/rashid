"""Cloud-native storage rules: COG validity/statistics and GeoParquet layout.

Needs the ``rashid[data]`` extra; skips without it. Drives the check functions
directly on generated assets — a spec-compliant asset produces no findings, and
each non-compliant variant raises exactly the rule it violates (formats.md:30/64/
75/91/95).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")

import pyarrow.parquet as pq  # noqa: E402

import rashid.data.checks as checks  # noqa: E402
from rashid.catalog import Node  # noqa: E402
from rashid.data import (  # noqa: E402
    DAT_COG,
    DAT_COG_STATS,
    DAT_GEOPARQUET_VERSION,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
    DAT_VECTOR_COLUMNS,
)
from rashid.data.reader import Locator  # noqa: E402
from rashid.model import Severity  # noqa: E402
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
    assert pq.ParquetFile(path).metadata.num_row_groups == 5
    defects = _gpq(path)
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.ERROR


def test_four_row_groups_skip_the_row_group_criteria(tmp_path: Path) -> None:
    """PORTO-FMT-044: below five row groups neither criterion can be expressed.

    These eight rows are the same interleaved layout the five-group file above
    is faulted for. They clear because the criteria do not apply at four groups
    and because eight rows are too few to chunk, not because they are ordered.
    """
    path = tmp_path / "four_groups.parquet"
    assets.write_geoparquet(path, points=assets.interleaved_points(8))
    assert pq.ParquetFile(path).metadata.num_row_groups == 4
    assert _gpq(path) == []


@pytest.mark.parametrize("groups", [2, 3, 4])
def test_row_ordering_is_judged_below_five_row_groups(tmp_path: Path, groups: int) -> None:
    """Row ordering binds every file, so the exemption does not reach it.

    The row-group criteria are skipped at these counts, and the rows are still
    partitioned into the chunks a conforming writer would have emitted. Globally
    scattered rows do not cluster in any of them.
    """
    rows = 6000
    path = tmp_path / f"scattered_{groups}.parquet"
    points = assets.scattered_points(rows)
    assets.write_geoparquet(path, points=points, row_group_size=rows // groups)
    assert pq.ParquetFile(path).metadata.num_row_groups == groups
    defects = _gpq(path)
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.ERROR
    assert f"in {groups} row groups" in defects[0].message


@pytest.mark.parametrize("groups", [2, 3, 4])
def test_curve_sorted_rows_are_clean_below_five_row_groups(tmp_path: Path, groups: int) -> None:
    """The same rows spatially sorted pass, so the check above is not vacuous."""
    rows = 6000
    path = tmp_path / f"sorted_{groups}.parquet"
    points = assets.hilbert_sorted(assets.scattered_points(rows))
    assets.write_geoparquet(path, points=points, row_group_size=rows // groups)
    assert pq.ParquetFile(path).metadata.num_row_groups == groups
    assert _gpq(path) == []


def test_five_row_groups_ordered_is_clean(tmp_path: Path) -> None:
    """The first count the criteria do apply at, on rows that satisfy them."""
    path = tmp_path / "five_groups.parquet"
    assets.write_geoparquet(path, points=assets.ordered_points(10))
    assert pq.ParquetFile(path).metadata.num_row_groups == 5
    assert _gpq(path) == []


@pytest.mark.parametrize("groups", [5, 6, 7])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_hilbert_sorted_rows_pass_where_the_criteria_apply(
    tmp_path: Path, groups: int, seed: int
) -> None:
    """The counts the 30% threshold was set from (formats.md:38).

    Hilbert-sorted boxes average 0.263 to 0.274 of the extent at five row groups
    and 0.250 to 0.270 at six, so the old 25% rejected them for their row-group
    count rather than their ordering. Consecutive boxes overlap at these counts,
    so low overlap never carries the file and the locality figure is what decides.
    """
    rows = 6300  # divisible by 5, 6, and 7
    path = tmp_path / f"hilbert_{groups}_{seed}.parquet"
    points = assets.hilbert_sorted(assets.scattered_points(rows, seed=seed))
    assets.write_geoparquet(path, points=points, row_group_size=rows // groups)
    assert pq.ParquetFile(path).metadata.num_row_groups == groups
    assert _gpq(path) == []


@pytest.mark.parametrize("groups", [5, 6, 7])
def test_unsorted_rows_still_fail_where_the_criteria_apply(tmp_path: Path, groups: int) -> None:
    """Raising the threshold to 30% does not let genuinely poor ordering through."""
    rows = 6300
    path = tmp_path / f"scattered_{groups}.parquet"
    assets.write_geoparquet(
        path, points=assets.scattered_points(rows), row_group_size=rows // groups
    )
    assert pq.ParquetFile(path).metadata.num_row_groups == groups
    defects = _gpq(path)
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.ERROR


def test_single_row_group_unordered_rows_flag_dat_006(tmp_path: Path) -> None:
    """The plain stac-geoparquet writer emits one row group at any row count.

    ``parse_stac_items_to_arrow`` returns one contiguous record batch unless a
    schema is passed, and ``to_parquet`` writes one row group per batch. With
    only one box to compare, the row-group criteria have nothing to measure
    and the MUST passes vacuously on entirely unsorted data.
    """
    path = tmp_path / "one_group.parquet"
    points = assets.scattered_points(5000)
    assets.write_geoparquet(path, points=points, row_group_size=len(points))
    defects = _gpq(path)
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.ERROR


def test_single_row_group_curve_sorted_rows_are_clean(tmp_path: Path) -> None:
    """The same rows, spatially sorted: nearby features are nearby in the file."""
    path = tmp_path / "one_group_sorted.parquet"
    points = assets.hilbert_sorted(assets.scattered_points(5000))
    assets.write_geoparquet(path, points=points, row_group_size=len(points))
    assert _gpq(path) == []


def test_small_single_row_group_collection_is_not_judged(tmp_path: Path) -> None:
    """Too few rows for chunking to mean anything, so no spurious finding."""
    path = tmp_path / "small.parquet"
    points = assets.local_points(20)
    assets.write_geoparquet(path, points=points, row_group_size=len(points))
    assert _gpq(path) == []


def test_single_row_group_without_readable_row_boxes_reports_unevaluated() -> None:
    """Honest silence, not a false pass, when the boxes cannot be read.

    A file whose per-row-group boxes come from native GeospatialStatistics has
    no covering column to read row by row, so ordering stays unmeasured and
    the check says so rather than reporting success.
    """
    defects = checks._row_ordering_defects("data", object(), {}, 5000, 1, report_unreadable=True)
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.INFO
    assert "could not be evaluated" in defects[0].message


def test_unreadable_boxes_stay_quiet_when_the_criteria_apply() -> None:
    """At five or more row groups the footer metrics measure ordering anyway,
    so an unreadable covering column is not a gap worth reporting."""
    defects = checks._row_ordering_defects("data", object(), {}, 5000, 8, report_unreadable=False)
    assert defects == []


@pytest.mark.parametrize(
    "geo",
    [
        pytest.param({}, id="no-columns"),
        pytest.param({"primary_column": "g", "columns": {"g": {}}}, id="no-covering"),
        pytest.param(
            {"primary_column": "g", "columns": {"g": {"covering": {"bbox": {"xmin": ["b", "x"]}}}}},
            id="incomplete-corners",
        ),
        pytest.param(
            {
                "primary_column": "g",
                "columns": {
                    "g": {
                        "covering": {
                            "bbox": {
                                "xmin": ["b", "deep", "x"],
                                "ymin": ["b", "deep", "y"],
                                "xmax": ["b", "deep", "X"],
                                "ymax": ["b", "deep", "Y"],
                            }
                        }
                    }
                },
            },
            id="nested-deeper-than-one-struct",
        ),
        pytest.param(
            {
                "primary_column": "g",
                "columns": {
                    "g": {
                        "covering": {
                            "bbox": {
                                "xmin": ["lo", "x"],
                                "ymin": ["lo", "y"],
                                "xmax": ["hi", "x"],
                                "ymax": ["hi", "y"],
                            }
                        }
                    }
                },
            },
            id="corners-split-across-two-structs",
        ),
    ],
)
def test_row_bboxes_declines_coverings_it_cannot_read(geo: dict) -> None:
    assert checks._row_bboxes(object(), geo) is None


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
    """Duck-types what _rowgroup_stat_defects touches: itself its own .metadata."""

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
    boxes, defects = checks._rowgroup_stat_defects("data", _FakeParquet(groups), _GEO_2X)
    assert boxes == ordered
    # The MUST is satisfied, but the covering column stays RECOMMENDED for
    # 2.x files even where native statistics exist (formats.md) — a WARNING.
    assert [d.severity for d in defects] == [Severity.WARNING]
    assert defects[0].rule_id == DAT_ROWGROUP_STATS
    assert "covering column" in defects[0].message


def test_absent_native_statistics_still_error() -> None:
    groups = [_FakeRowGroup([_FakeColumn("geometry", None)])]
    boxes, defects = checks._rowgroup_stat_defects("data", _FakeParquet(groups), _GEO_2X)
    assert boxes is None
    assert [d.severity for d in defects] == [Severity.ERROR]
    assert defects[0].rule_id == DAT_ROWGROUP_STATS


class _OldPyarrowColumn:
    """pyarrow < 21 column metadata: no geo_statistics attribute at all."""

    def __init__(self, path: str) -> None:
        self.path_in_schema = path


def test_old_pyarrow_without_geo_statistics_attr_still_errors() -> None:
    """On pyarrow < 21 the attribute is missing entirely; the reader must fall
    through to "no native source" (ERROR), not crash on the absent attribute."""
    groups = [_FakeRowGroup([_OldPyarrowColumn("geometry")])]  # type: ignore[list-item]
    boxes, defects = checks._rowgroup_stat_defects("data", _FakeParquet(groups), _GEO_2X)
    assert boxes is None
    assert [d.severity for d in defects] == [Severity.ERROR]
    assert defects[0].rule_id == DAT_ROWGROUP_STATS


def test_oversized_rowgroup_flags_dat_008(tmp_path: Path) -> None:
    path = tmp_path / "big.parquet"
    assets.write_geoparquet(path, points=assets.ordered_points(150_001), row_group_size=200_000)
    defects = _gpq(path)
    assert DAT_ROWGROUP_SIZE in [d.rule_id for d in defects]
    assert next(d for d in defects if d.rule_id == DAT_ROWGROUP_SIZE).severity is Severity.ERROR


def test_sorted_file_split_for_the_ceiling_stays_clean(tmp_path: Path) -> None:
    """The shape PTL-DAT-008 forces on a file just over the ceiling.

    150,001 rows in groups of 50,000 is four row groups, and four boxes off a
    curve sort average about 46% of the extent. The flat 25% limit read that as
    unordered, so satisfying one storage rule produced a finding under another.
    """
    path = tmp_path / "ceiling.parquet"
    points = assets.hilbert_sorted(assets.scattered_points(150_001))
    assets.write_geoparquet(path, points=points, row_group_size=50_000)
    assert pq.ParquetFile(path).metadata.num_row_groups == 4
    assert _gpq(path) == []


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


# --- partition bytes (PTL-DAT-006/007/008/012 over the glob) -----------------
#
# A partitioned collection carries no data asset, so the per-asset loop reaches
# none of its bytes. These drive the pass that walks the glob instead.


class _NoAssets:
    """A reader for a collection that declares no asset."""

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        return None

    def locate(self, node: Node, href: str) -> Locator | None:
        return None


def _partition_bytes(node: Node, reader: object | None = None) -> list:
    return checks._check_partition_geoparquet(node, reader or _NoAssets())


def test_conformant_partitions_are_clean(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet")
    assert _partition_bytes(_partitioned_collection(tmp_path)) == []


def test_oversized_partition_row_group_flags_dat_008(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(
        parts / "b.parquet", points=assets.ordered_points(150_001), row_group_size=200_000
    )
    defects = _partition_bytes(_partitioned_collection(tmp_path))
    assert [d.rule_id for d in defects] == [DAT_ROWGROUP_SIZE]
    assert defects[0].severity is Severity.ERROR
    assert defects[0].json_pointer == "/partition:glob"
    assert defects[0].message.startswith("partition file 'parts/b.parquet' has a row group")


def test_legacy_geoparquet_partition_flags_dat_012(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet", version="1.0.0")
    ids = [d.rule_id for d in _partition_bytes(_partitioned_collection(tmp_path))]
    assert DAT_GEOPARQUET_VERSION in ids


def test_partition_without_rowgroup_stats_flags_dat_007(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet", covering=False)
    defects = _partition_bytes(_partitioned_collection(tmp_path))
    assert [d.rule_id for d in defects] == [DAT_ROWGROUP_STATS]
    assert defects[0].severity is Severity.ERROR


def test_unordered_partition_rows_flag_dat_006(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet", points=assets.interleaved_points())
    defects = _partition_bytes(_partitioned_collection(tmp_path))
    assert [d.rule_id for d in defects] == [DAT_ORDERING]
    assert defects[0].severity is Severity.ERROR


def test_one_defect_reports_the_file_alone(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet", version="1.0.0")
    defects = _partition_bytes(_partitioned_collection(tmp_path))
    message = next(d for d in defects if d.rule_id == DAT_GEOPARQUET_VERSION).message
    assert message.startswith("partition file 'parts/b.parquet' geo metadata")
    assert "partition files fail" not in message


def test_many_failing_partitions_fold_into_one_defect(tmp_path: Path) -> None:
    """Hundreds of partitions come from one job, so one bad setting reports once."""
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    for name in ("b", "c", "d"):
        assets.write_geoparquet(parts / f"{name}.parquet", version="1.0.0")
    defects = [
        d
        for d in _partition_bytes(_partitioned_collection(tmp_path))
        if d.rule_id == DAT_GEOPARQUET_VERSION
    ]
    assert len(defects) == 1
    assert defects[0].message.startswith("3 of 4 partition files fail this check;")
    assert "partition file 'parts/b.parquet'" in defects[0].message


def test_partition_severities_fold_separately(tmp_path: Path) -> None:
    """An ERROR and a WARNING of one rule stay two findings, not one."""
    defects = [
        checks.DataDefect(DAT_ROWGROUP_STATS, Severity.ERROR, "e", "x"),
        checks.DataDefect(DAT_ROWGROUP_STATS, Severity.WARNING, "w", "x"),
    ]
    folded = checks._fold_partition_defects(defects, 2)
    assert [d.severity for d in folded] == [Severity.ERROR, Severity.WARNING]


def test_plain_parquet_partitions_are_left_alone(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet", geo=False, covering=False)
    assets.write_geoparquet(parts / "b.parquet", geo=False, covering=False)
    assert _partition_bytes(_partitioned_collection(tmp_path)) == []


def test_unreadable_partition_is_silent(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "a.parquet").write_bytes(b"not parquet")
    assert _partition_bytes(_partitioned_collection(tmp_path)) == []


@pytest.mark.parametrize(
    "glob",
    ["s3://bucket/parts/*.parquet", "https://example.org/parts/*.parquet", "/abs/*.parquet"],
)
def test_unlistable_partition_glob_is_skipped(tmp_path: Path, glob: str) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet", version="1.0.0")
    assert _partition_bytes(_partitioned_collection(tmp_path, glob=glob)) == []


def test_escaping_partition_glob_reads_no_bytes(tmp_path: Path) -> None:
    node = _partitioned_collection(tmp_path, glob="../elsewhere/*.parquet")
    assert _partition_bytes(node) == []


def test_collection_without_a_glob_reads_no_bytes(tmp_path: Path) -> None:
    node = _partitioned_collection(tmp_path)
    del node.data["partition:glob"]
    assert _partition_bytes(node) == []


def test_a_file_that_is_also_an_asset_reports_once(tmp_path: Path) -> None:
    """A glob wide enough to match a declared asset must not fault it twice."""
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet", version="1.0.0")
    node = _partitioned_collection(tmp_path)
    node.data["assets"] = {
        "data": {"href": "./parts/a.parquet", "type": "application/vnd.apache.parquet"}
    }
    reader = _FileReader("./parts/a.parquet", parts / "a.parquet")
    assert _partition_bytes(node, reader) == []
    ids = [d.rule_id for d in checks.check_node(node, reader)]
    assert ids.count(DAT_GEOPARQUET_VERSION) == 1


def test_assetless_partitioned_collection_reports_through_check_node(tmp_path: Path) -> None:
    """Issue #130: the shape formats.md prescribes reached none of these checks."""
    parts = tmp_path / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(
        parts / "b.parquet", points=assets.ordered_points(150_001), row_group_size=200_000
    )
    node = _partitioned_collection(tmp_path)
    assert node.data.get("assets") is None
    defects = checks.check_node(node, _NoAssets())
    assert [d.rule_id for d in defects] == [DAT_ROWGROUP_SIZE]


# --- tabular collections (PTL-DAT-015) --------------------------------------


def _tabular_collection(**overrides: object) -> Node:
    data: dict = {
        "type": "Collection",
        "id": "tables",
        "extent": {"spatial": {"bbox": [[4.0, 50.0, 6.0, 52.0]]}},
    }
    data.update(overrides)
    return Node(
        path=PurePosixPath("tables/collection.json"),
        abs_path=Path("/nowhere/collection.json"),
        kind="collection",
        id="tables",
        data=data,
    )


def _tabular(node: Node, path: Path) -> list:
    asset = {"href": "./data.parquet", "roles": ["data"]}
    return checks._check_tabular(node, "data", asset, _loc(path))


def _timestamp_column() -> dict[str, list[object]]:
    return {"observed": [datetime(2024, 1, 1) + timedelta(days=i) for i in range(6)]}


def test_tabular_without_table_columns_flags_dat_015(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False)
    defects = _tabular(_tabular_collection(), path)
    assert [d.rule_id for d in defects] == [DAT_TABULAR]
    assert defects[0].severity is Severity.WARNING
    assert "table:columns" in defects[0].message


def test_tabular_with_table_columns_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False)
    node = _tabular_collection(
        **{"table:columns": [{"name": "value", "type": "int64", "description": "a value"}]}
    )
    assert _tabular(node, path) == []


def test_tabular_with_asset_level_table_columns_is_clean(tmp_path: Path) -> None:
    # The table extension allows table:columns on the asset as well as on
    # the object carrying it.
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False)
    asset = {
        "href": "./data.parquet",
        "roles": ["data"],
        "table:columns": [{"name": "value", "type": "int64", "description": "a value"}],
    }
    assert checks._check_tabular(_tabular_collection(), "data", asset, _loc(path)) == []


def test_tabular_temporal_column_without_extent_flags_dat_015(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False, columns=_timestamp_column())
    node = _tabular_collection(**{"table:columns": [{"name": "observed"}]})
    defects = _tabular(node, path)
    assert [d.rule_id for d in defects] == [DAT_TABULAR]
    assert "extent.temporal" in defects[0].message


def test_tabular_temporal_column_with_extent_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False, columns=_timestamp_column())
    node = _tabular_collection(
        **{
            "table:columns": [{"name": "observed"}],
            "extent": {
                "spatial": {"bbox": [[4.0, 50.0, 6.0, 52.0]]},
                "temporal": {"interval": [["2024-01-01T00:00:00Z", None]]},
            },
        }
    )
    assert _tabular(node, path) == []


def test_geoparquet_is_not_tabular(tmp_path: Path) -> None:
    # A 'geo' metadata key marks GeoParquet: the geospatial rules own it and
    # the tabular SHOULDs stay silent.
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    assert _tabular(_tabular_collection(), path) == []


def test_non_data_roles_are_not_tabular(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False)
    asset = {"href": "./data.parquet", "roles": ["metadata"]}
    assert checks._check_tabular(_tabular_collection(), "data", asset, _loc(path)) == []


def test_item_level_parquet_is_not_tabular(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False)
    node = _node_with_asset({"href": "./data.parquet", "roles": ["data"]})
    assert checks._check_tabular(node, "data", node.data["assets"]["a"], _loc(path)) == []


# --- vector column documentation (PTL-DAT-017) ------------------------------


_COLUMNS = [{"name": "value", "type": "int64", "description": "a value"}]


def _vector_item(**overrides: object) -> Node:
    data: dict = {
        "type": "Feature",
        "id": "points",
        "properties": {"datetime": "2024-01-01T00:00:00Z"},
    }
    data.update(overrides)
    return Node(
        path=PurePosixPath("tables/points/points.json"),
        abs_path=Path("/nowhere/points.json"),
        kind="item",
        id="points",
        data=data,
    )


def _vector(node: Node, path: Path, **asset_fields: object) -> list:
    asset = {"href": "./data.parquet", "roles": ["data"], **asset_fields}
    return checks._check_vector_columns(node, "data", asset, _loc(path))


def test_vector_collection_without_table_columns_flags_dat_017(tmp_path: Path) -> None:
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    defects = _vector(_tabular_collection(), path)
    assert [d.rule_id for d in defects] == [DAT_VECTOR_COLUMNS]
    assert defects[0].severity is Severity.WARNING
    assert "the collection does not document its columns" in defects[0].message


def test_vector_collection_with_table_columns_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    assert _vector(_tabular_collection(**{"table:columns": _COLUMNS}), path) == []


def test_vector_collection_with_asset_level_table_columns_is_clean(tmp_path: Path) -> None:
    # PORTO-FMT-048: a collection whose data assets describe differing schemas
    # MAY declare table:columns per asset instead of on the collection.
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    assert _vector(_tabular_collection(), path, **{"table:columns": _COLUMNS}) == []


def test_vector_item_without_table_columns_flags_dat_017(tmp_path: Path) -> None:
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    defects = _vector(_vector_item(), path)
    assert [d.rule_id for d in defects] == [DAT_VECTOR_COLUMNS]
    assert "the item does not document its columns in properties" in defects[0].message


def test_vector_item_with_table_columns_in_properties_is_clean(tmp_path: Path) -> None:
    # PORTO-FMT-047 asks for the field in properties, where an item's fields
    # live, not at the top level.
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    properties = {"datetime": "2024-01-01T00:00:00Z", "table:columns": _COLUMNS}
    assert _vector(_vector_item(properties=properties), path) == []


def test_plain_parquet_is_not_vector(tmp_path: Path) -> None:
    # No 'geo' metadata key: the Tabular Data SHOULDs own it and PTL-DAT-017
    # stays silent, which is the mirror of test_geoparquet_is_not_tabular.
    path = tmp_path / "plain.parquet"
    assets.write_geoparquet(path, geo=False)
    assert _vector(_tabular_collection(), path) == []


def test_non_data_roles_are_not_vector(tmp_path: Path) -> None:
    path = tmp_path / "geo.parquet"
    assets.write_geoparquet(path)
    asset = {"href": "./data.parquet", "roles": ["metadata"]}
    assert checks._check_vector_columns(_tabular_collection(), "data", asset, _loc(path)) == []
