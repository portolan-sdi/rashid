"""Byte-check logic — fake readers with in-memory bytes, no network.

Needs the ``rashid[data]`` extra (importing :mod:`rashid.data.checks` pulls the
geospatial stack), so it lives under integration and skips when the extra is
absent. Covers the checksum, size, and format checks with synthetic bytes plus
the PMTiles header parse and the reprojection helper; the parquet-geo and COG
checks are exercised end-to-end in ``test_data_catalog``.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("pyproj")

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS, Transformer

import rashid.data.checks as checks
from rashid import _trace
from rashid.catalog import Node
from rashid.data import DataDefect, DataResults, geoparquet, mirror, raster, spatial, storage
from rashid.data._files import AssetFiles
from rashid.data.reader import Locator
from rashid.model import Severity
from rashid.registry import DAT_CHECKSUM, DAT_COG, DAT_CONSISTENCY, DAT_FORMAT, DAT_SIZE
from rashid.rules._common import is_source_asset

# --- the shared test vectors -------------------------------------------------

_UNIT = (0.0, 0.0, 1.0, 1.0)

_VECTORS = json.loads(
    (
        Path(__file__).resolve().parent.parent / "fixtures" / "spatial-metric-vectors.json"
    ).read_text()
)


pytestmark = pytest.mark.integration

_PARQUET = "application/vnd.apache.parquet"


def _multihash(payload: bytes, code: str = "12") -> str:
    return code + "20" + hashlib.sha256(payload).hexdigest()


class _FakeReader:
    """Serves canned bytes for one href; locates nothing (skips geo/COG)."""

    def __init__(self, href: str, payload: bytes) -> None:
        self._href = href
        self._payload = payload

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        return iter([self._payload]) if href == self._href else None

    def locate(self, node: Node, href: str) -> Locator | None:
        return None


def _item(asset: dict[str, object]) -> Node:
    return Node(
        path=PurePosixPath("roads/seg1/seg1.json"),
        abs_path=Path("/nowhere/seg1.json"),
        kind="item",
        id="seg1",
        data={"type": "Feature", "bbox": [4.0, 50.0, 6.0, 52.0], "assets": {"data": asset}},
    )


def _run(payload: bytes, asset: dict[str, object]) -> list:
    node = _item(asset)
    return checks.check_node(node, _FakeReader("./data.parquet", payload))


def _asset(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "href": "./data.parquet",
        "type": _PARQUET,
        "roles": ["data"],
    }
    base.update(over)
    return base


def test_matching_bytes_are_clean() -> None:
    payload = b"PAR1" + b"\x00" * 100 + b"PAR1"
    asset = _asset(**{"file:size": len(payload), "file:checksum": _multihash(payload)})
    assert _run(payload, asset) == []


def test_checksum_mismatch_is_error() -> None:
    payload = b"PAR1data"
    asset = _asset(**{"file:size": len(payload), "file:checksum": _multihash(b"different")})
    defects = _run(payload, asset)
    assert [d.rule_id for d in defects] == [DAT_CHECKSUM]
    assert defects[0].severity is Severity.ERROR
    assert defects[0].field == "file:checksum"


def test_size_mismatch_is_error() -> None:
    payload = b"PAR1data"
    asset = _asset(**{"file:size": 999, "file:checksum": _multihash(payload)})
    defects = _run(payload, asset)
    assert [d.rule_id for d in defects] == [DAT_SIZE]
    assert "999" in defects[0].message


def test_format_mismatch_is_error() -> None:
    payload = b"PAR1data"  # real parquet magic
    asset = _asset(type="application/vnd.pmtiles", **{"file:size": len(payload)})
    defects = _run(payload, asset)
    assert [d.rule_id for d in defects] == [DAT_FORMAT]
    assert "pmtiles" in defects[0].message and "parquet" in defects[0].message


def test_unsupported_hash_is_info_not_error() -> None:
    payload = b"PAR1data"
    # 0x18 = keccak-256, valid multihash code rashid cannot compute.
    digest = "00" * 32
    asset = _asset(**{"file:size": len(payload), "file:checksum": "1820" + digest})
    defects = _run(payload, asset)
    assert [d.rule_id for d in defects] == [DAT_CHECKSUM]
    assert defects[0].severity is Severity.INFO


def test_unreadable_stream_is_info() -> None:
    class _ExplodingStream:
        def __iter__(self) -> _ExplodingStream:
            return self

        def __next__(self) -> bytes:
            raise OSError("connection reset")

    class _Boom:
        def stream(self, node: Node, href: str) -> Iterator[bytes]:
            return _ExplodingStream()

        def locate(self, node: Node, href: str) -> Locator | None:
            return None

    defects = checks.check_node(_item(_asset()), _Boom())
    assert [d.rule_id for d in defects] == [DAT_CHECKSUM]
    assert defects[0].severity is Severity.INFO


def test_absent_checksum_and_size_are_skipped() -> None:
    payload = b"PAR1data"
    defects = _run(payload, _asset())  # no file:size / file:checksum
    assert defects == []  # PTL-AST-003 owns absence; the data pass stays silent


def test_pmtiles_header_bbox(tmp_path: Path) -> None:
    header = bytearray(127)
    header[0:7] = b"PMTiles"
    header[7] = 3
    struct.pack_into("<iiii", header, 102, int(4.0e7), int(50.0e7), int(6.0e7), int(52.0e7))
    path = tmp_path / "tiles.pmtiles"
    path.write_bytes(bytes(header))

    geo = spatial.geo_from_pmtiles(Locator(is_remote=False, source=str(path)))

    assert geo is not None
    assert geo.epsg == 4326
    assert geo.bbox == pytest.approx([4.0, 50.0, 6.0, 52.0])


# The netherlands-provinces GeoParquet of the spec's reference catalog: a native
# EPSG:28992 (RD New) bbox and the exact WGS84 envelope of its geometries, taken
# by reprojecting every geometry. RD New is oblique stereographic, so a constant
# northing bows north toward the projection origin and the corners of the native
# rectangle are the *lowest* points on its north edge.
_RD_NEW_BBOX = [10425.156, 306846.198, 278026.09, 621876.3]
_RD_NEW_ENVELOPE = [3.307938, 50.750367, 7.227498, 53.576423]


def _rd_new_geo() -> spatial.Geo:
    return spatial.Geo(bbox=list(_RD_NEW_BBOX), epsg=28992, crs=CRS.from_epsg(28992))


def test_wgs84_bounds_of_unprojected_bbox_are_exact() -> None:
    bbox = [4.0, 50.0, 6.0, 52.0]
    geo = spatial.Geo(bbox=bbox, epsg=4326, crs=CRS.from_epsg(4326))

    bounds = spatial.wgs84_bounds(geo)

    assert bounds is not None
    assert not bounds.reprojected
    assert bounds.outer == pytest.approx(bbox)
    assert bounds.inner == pytest.approx(bbox)


def test_wgs84_bounds_reproject_a_projected_bbox() -> None:
    to_mercator = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(3857), always_xy=True)
    minx, miny = to_mercator.transform(4.0, 50.0)
    maxx, maxy = to_mercator.transform(6.0, 52.0)
    geo = spatial.Geo(bbox=[minx, miny, maxx, maxy], epsg=3857, crs=CRS.from_epsg(3857))

    bounds = spatial.wgs84_bounds(geo)

    assert bounds is not None
    assert bounds.reprojected
    # web mercator keeps meridians and parallels straight, so both bounds close
    # in on the same box from either side
    assert bounds.outer == pytest.approx([4.0, 50.0, 6.0, 52.0], abs=1e-6)
    assert bounds.inner == pytest.approx([4.0, 50.0, 6.0, 52.0], abs=1e-6)


def test_wgs84_bounds_bracket_the_true_envelope() -> None:
    bounds = spatial.wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert bounds.inner is not None
    minx, miny, maxx, maxy = _RD_NEW_ENVELOPE
    # outer contains the true envelope, inner is contained by it, strictly on
    # every side — a non-affine projection admits no tighter claim
    assert bounds.outer[0] < minx < bounds.inner[0]
    assert bounds.outer[1] < miny < bounds.inner[1]
    assert bounds.inner[2] < maxx < bounds.outer[2]
    assert bounds.inner[3] < maxy < bounds.outer[3]


def test_projected_bbox_accepts_its_true_envelope() -> None:
    """Reprojecting only the corners under-claims the north edge here (#26)."""
    bounds = spatial.wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert spatial.bbox_within(_RD_NEW_ENVELOPE, bounds)


@pytest.mark.parametrize(
    "over",
    [
        [3.0, 50.75, 7.22, 53.57],  # west of any longitude the data can reach
        [3.31, 50.5, 7.22, 53.9],  # south and north of any latitude it can reach
    ],
)
def test_projected_bbox_rejects_an_over_claimed_declaration(over: list[float]) -> None:
    bounds = spatial.wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert not spatial.bbox_within(over, bounds)


def test_projected_mismatch_names_both_bounds() -> None:
    bounds = spatial.wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert bounds.inner is not None
    message = spatial.bbox_mismatch_message("data", [4.0, 51.0, 6.0, 53.0], bounds, 28992)

    assert "EPSG:28992" in message
    assert spatial._fmt_bbox(bounds.inner) in message
    assert spatial._fmt_bbox(bounds.outer) in message


def test_projected_bbox_rejects_an_under_claimed_declaration() -> None:
    """A stale bbox covering part of the data must still warn."""
    bounds = spatial.wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    under = [4.0, 51.0, 6.0, 53.0]
    assert not spatial.bbox_within(under, bounds)


@pytest.mark.parametrize("epsg", [4326, 28992])
def test_wgs84_bounds_of_an_untight_bbox_have_no_inner_side(epsg: int) -> None:
    """A raster grid only contains its data, so no side is pinned from inside."""
    bbox = [4.0, 50.0, 6.0, 52.0] if epsg == 4326 else list(_RD_NEW_BBOX)
    geo = spatial.Geo(bbox=bbox, epsg=epsg, crs=CRS.from_epsg(epsg), tight=False)

    bounds = spatial.wgs84_bounds(geo)

    assert bounds is not None
    assert bounds.inner is None


def test_untight_bbox_accepts_a_declaration_inside_the_collar() -> None:
    """A nodata collar puts the real footprint well inside the grid extent."""
    grid = spatial.Geo(bbox=[4.0, 50.0, 6.0, 52.0], epsg=4326, crs=CRS.from_epsg(4326), tight=False)
    bounds = spatial.wgs84_bounds(grid)

    assert bounds is not None
    assert spatial.bbox_within([4.8, 50.9, 5.1, 51.2], bounds)
    assert not spatial.bbox_within([3.5, 50.9, 5.1, 51.2], bounds)


def test_untight_mismatch_names_the_containing_extent() -> None:
    bounds = spatial.Wgs84Bounds(outer=[4.0, 50.0, 6.0, 52.0], inner=None)

    message = spatial.bbox_mismatch_message("data", [3.5, 50.9, 5.1, 51.2], bounds, 4326)

    assert "is not contained by the asset's extent" in message


def test_wgs84_bounds_skip_an_antimeridian_crossing() -> None:
    """Straddling the seam, min/max in degrees describes no box at all."""
    # PDC Mercator, central meridian 150E; the seam sits near x=3.34e6.
    geo = spatial.Geo(bbox=[3.0e6, -1.0e6, 5.0e6, 1.0e6], epsg=3832, crs=CRS.from_epsg(3832))

    assert spatial.wgs84_bounds(geo) is None


@pytest.mark.parametrize("epsg", [3035, 32631])
def test_wgs84_bounds_skip_an_unprojectable_bbox(epsg: int) -> None:
    """Coordinates outside the projection's domain reproject to infinity."""
    geo = spatial.Geo(bbox=[-1e9, -1e9, 1e9, 1e9], epsg=epsg, crs=CRS.from_epsg(epsg))

    assert spatial.wgs84_bounds(geo) is None


def test_consistency_skips_the_bbox_it_cannot_reproject(monkeypatch: pytest.MonkeyPatch) -> None:
    geo = spatial.Geo(bbox=[3.0e6, -1.0e6, 5.0e6, 1.0e6], epsg=3832, crs=CRS.from_epsg(3832))
    monkeypatch.setattr(checks, "_extract_geo", lambda expected, files: geo)
    located = Locator(is_remote=False, source="unused")
    files = AssetFiles(located)

    defects, _bounds, _complete = checks._check_consistency(
        _item(_asset()),
        key="data",
        asset=_asset(),
        expected="parquet",
        files=files,
    )

    assert defects == []


def test_bbox_within_tolerance() -> None:
    exact = spatial.Wgs84Bounds(outer=[4.0, 50.0, 6.0, 52.0], inner=[4.0, 50.0, 6.0, 52.0])
    assert spatial.bbox_within([4.005, 50.0, 6.0, 52.0], exact)
    assert not spatial.bbox_within([4.5, 50.0, 6.0, 52.0], exact)


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff\xe0", "jpeg"),
        (b"MM\x00*rest", "tiff"),
        (b"nothing", None),
    ],
)
def test_detect_format_variants(head: bytes, expected: str | None) -> None:
    assert storage._detect_format(head) == expected


@pytest.mark.parametrize(
    "media,expected",
    [
        ("image/png", "png"),
        ("image/jpeg", "jpeg"),
        ("image/tiff; application=geotiff", "tiff"),
        ("application/vnd.pmtiles", "pmtiles"),
        ("application/json", None),
    ],
)
def test_expected_format_variants(media: str, expected: str | None) -> None:
    assert storage.expected_format(media) == expected


@pytest.mark.parametrize(
    "raw,out",
    [
        ([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]),
        ([1.0, 2.0, 9.0, 3.0, 4.0, 9.0], [1.0, 2.0, 3.0, 4.0]),  # drop z
        ([1.0, 2.0], None),
        (["x", "y", "z", "w"], None),
        ("nope", None),
    ],
)
def test_as_bbox(raw: object, out: list[float] | None) -> None:
    assert spatial.as_bbox(raw) == out


def test_declared_bbox_collection_extent() -> None:
    node = Node(
        path=PurePosixPath("c/collection.json"),
        abs_path=Path("/x"),
        kind="collection",
        id="c",
        data={"extent": {"spatial": {"bbox": [[4.0, 50.0, 6.0, 52.0]]}}},
    )
    assert spatial.declared_bbox(node) == [4.0, 50.0, 6.0, 52.0]


def test_declared_epsg_from_properties() -> None:
    node = Node(
        path=PurePosixPath("c/i/i.json"),
        abs_path=Path("/x"),
        kind="item",
        id="i",
        data={"properties": {"proj:epsg": 3857}},
    )
    inherited = spatial.declared_epsg(node, {})
    assert inherited == spatial._EpsgDeclaration(3857, "/properties/proj:epsg")
    own = spatial.declared_epsg(node, {"proj:epsg": 32631})  # asset wins
    assert own == spatial._EpsgDeclaration(32631, None)


def test_declared_epsg_from_document_root() -> None:
    node = Node(
        path=PurePosixPath("c/collection.json"),
        abs_path=Path("/x"),
        kind="collection",
        id="c",
        data={"proj:epsg": 3857},
    )
    assert spatial.declared_epsg(node, {}) == spatial._EpsgDeclaration(3857, "/proj:epsg")


def test_declared_epsg_absent() -> None:
    node = Node(path=PurePosixPath("c/i/i.json"), abs_path=Path("/x"), kind="item", id="i", data={})
    assert spatial.declared_epsg(node, {}) is None


def test_inherited_epsg_defect_points_at_the_declaration() -> None:
    node = Node(
        path=PurePosixPath("c/collection.json"),
        abs_path=Path("/x"),
        kind="collection",
        id="c",
        data={"proj:epsg": 3857},
    )
    defect = spatial.epsg_defect(node, "data", spatial._EpsgDeclaration(3857, "/proj:epsg"), 4326)
    assert defect.json_pointer == "/proj:epsg"
    assert defect.expected == 4326
    assert defect.actual == 3857
    assert "collection 'c'" in defect.message
    assert "inherit" in defect.message


def test_asset_epsg_defect_points_at_the_asset() -> None:
    node = _item(_asset())
    defect = spatial.epsg_defect(node, "data", spatial._EpsgDeclaration(3857, None), 4326)
    assert defect.json_pointer is None  # validate_data points it at /assets/data
    assert defect.asset_key == "data"
    assert defect.message.startswith("asset 'data' declares proj:epsg 3857")


def test_collapse_keeps_one_defect_per_shared_field() -> None:
    inherited = [
        DataDefect(DAT_CONSISTENCY, Severity.WARNING, "same field", key, json_pointer="/proj:epsg")
        for key in ("data", "extra", "third")
    ]
    per_asset = [
        DataDefect(DAT_CONSISTENCY, Severity.WARNING, f"asset {key}", key)
        for key in ("data", "extra")
    ]
    source = DataResults()
    source.extend(inherited + per_asset)
    collapsed = source.build()
    assert [d.asset_key for d in collapsed] == ["data", "data", "extra"]


def test_check_raster_reader_error_is_info() -> None:
    located = Locator(is_remote=False, source="/no/such/file.tif")
    defects = raster.check_raster("data", located, AssetFiles(located))
    assert [d.rule_id for d in defects] == [DAT_COG]
    assert defects[0].severity is Severity.INFO


def test_geo_from_parquet_without_geo_metadata(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), path)
    files = AssetFiles(Locator(is_remote=False, source=str(path)))
    assert geoparquet.geo_from_parquet(files) is None


def test_consistency_unreadable_is_info() -> None:
    node = _item(_asset())
    located = Locator(is_remote=False, source="/no/such/file.parquet")
    files = AssetFiles(located)
    defects, _bounds, _complete = checks._check_consistency(
        node, key="data", asset=_asset(), expected="parquet", files=files
    )
    assert [d.rule_id for d in defects] == [DAT_CONSISTENCY]
    assert defects[0].severity is Severity.INFO


@pytest.mark.parametrize("opener", ["parquet", "raster"])
def test_asset_files_open_once_and_repeat_a_failure(opener: str) -> None:
    """Every check sees the one exception the one open raised."""
    files = AssetFiles(Locator(is_remote=False, source="/no/such/file"))
    raised = []
    with _trace.counting() as delta:
        for _ in range(3):
            with pytest.raises(
                (OSError, pa.ArrowInvalid), match=r"[/\\]no[/\\]such[/\\]file"
            ) as caught:
                getattr(files, opener)()
            raised.append(caught.value)
    assert raised[0] is raised[1] is raised[2]
    assert delta == {f"{opener}.opens": 1}


class _LocalReader(_FakeReader):
    """Serves one local file's bytes and locates it, so the format checks run."""

    def __init__(self, href: str, path: Path) -> None:
        super().__init__(href, path.read_bytes())
        self._path = path

    def locate(self, node: Node, href: str) -> Locator | None:
        return Locator(is_remote=False, source=str(self._path)) if href == self._href else None


def test_unreadable_parquet_opens_once_and_reports_as_before(tmp_path: Path) -> None:
    """A footer that will not parse is opened once, and only the consistency check reports it.

    The GeoParquet and vector-column checks stay silent on an unreadable file,
    as they did when each opened it for itself; the byte checks own it.
    """
    path = tmp_path / "data.parquet"
    path.write_bytes(b"PAR1" + b"\x00" * 64 + b"PAR1")

    with _trace.counting() as delta:
        defects = checks.check_node(_item(_asset()), _LocalReader("./data.parquet", path))

    assert delta["parquet.opens"] == 1
    assert [(d.rule_id, d.severity) for d in defects] == [(DAT_CONSISTENCY, Severity.INFO)]
    assert "spatial metadata could not be read" in defects[0].message


def test_spatial_ordering_low_overlap() -> None:
    disjoint = [(0.0, 0.0, 1.0, 1.0), (2.0, 2.0, 3.0, 3.0), (4.0, 4.0, 5.0, 5.0)]
    assert not geoparquet._pruning_score(disjoint).below_bar


def test_spatial_ordering_high_overlap_fails() -> None:
    piled = [(0.0, 0.0, 5.0, 5.0)] * 4
    assert geoparquet._pruning_score(piled).below_bar


def test_spatial_ordering_high_locality_despite_overlap() -> None:
    # Every consecutive pair overlaps (low-overlap fails), but each box is a small
    # fraction of the extent, so a query window skips most of them.
    boxes = [(float(i), 0.0, float(i) + 2.0, 1.0) for i in range(10)]
    assert not all(  # sanity: neighbours really do overlap
        not geoparquet._bbox_overlaps(boxes[i], boxes[i + 1]) for i in range(len(boxes) - 1)
    )
    assert not geoparquet._pruning_score(boxes).below_bar


def test_spatial_ordering_needs_a_skippable_layout() -> None:
    # Row groups that all but span the extent. Every consecutive pair overlaps,
    # and each box covers nearly the whole extent, so a reader skips none of
    # them.
    spanning = [(0.0, 0.0, 10.0, 10.0)] + [(0.05 * i, 0.05 * i, 10.0, 10.0) for i in range(1, 5)]
    assert geoparquet._pruning_score(spanning).below_bar


def _assert_expected(score: geoparquet._PruningScore, expected: dict) -> None:
    assert score.count == expected["count"]
    assert score.achieved == pytest.approx(expected["achieved"], abs=1e-6)
    assert score.achievable == pytest.approx(expected["achievable"], abs=1e-6)
    if expected["efficiency"] is None:
        assert score.efficiency is None
    else:
        assert score.efficiency == pytest.approx(expected["efficiency"], abs=1e-6)
    if expected["area_sum"] is None:
        assert score.area_sum is None  # 0/0 on a zero-area extent: undefined, never 0.0
    else:
        assert score.area_sum == pytest.approx(expected["area_sum"], abs=1e-6)
    assert score.below_bar is expected["below_bar"]


def test_an_unboxed_chunk_shrinks_the_reference() -> None:
    """n is the number of chunks that carry a box (PORTO-FMT-006's test, spec 2555a86).

    Twenty-three rows whose chunks 2 and 5 hold only null boxes: eight chunk
    boxes are scored against the eight-cell grid, not the ten-cell one.
    """
    rows: list = [(float(i), 0.0, float(i) + 1.0, 1.0) for i in range(23)]
    for i in (4, 5, 11, 12):
        rows[i] = None
    chunks, weights = geoparquet._chunked_bboxes(rows)
    assert len(chunks) == 8
    score = geoparquet._pruning_score(chunks, weights, weights)
    extent = spatial.bbox_union(chunks)
    eight = geoparquet._ideal_grid_boxes(extent, 8)
    ten = geoparquet._ideal_grid_boxes(extent, 10)
    assert score.achievable == geoparquet._weighted_skip_rate(eight, [1.0] * 8, [1.0] * 8)
    assert score.achievable != geoparquet._weighted_skip_rate(ten, [1.0] * 10, [1.0] * 10)


def test_area_sum_is_the_zero_window_expectation() -> None:
    # Two boxes each a quarter of the extent: their areas sum to half of it.
    halves = [(0.0, 0.0, 0.5, 0.5), (0.5, 0.5, 1.0, 1.0)]
    assert geoparquet._pruning_score(halves).area_sum == pytest.approx(0.5)


def test_closed_form_matches_the_sampled_mean() -> None:
    """Cross-check 3: the expectation is the quantity a window sample estimates.

    Draw a box by its rows, a centre uniform in it, a window at 10% of that
    box, and count the bytes of the boxes it misses: the mean over many draws
    converges to the closed form, on both the actual boxes and the reference.
    A sampled rule moved its verdict with the seed on real files, which is why
    the spec states the limit instead.
    """
    import random

    boxes = [(0.0, 0.0, 0.3, 0.3), (0.2, 0.1, 0.6, 0.5), (0.5, 0.5, 1.0, 1.0), (0.0, 0.6, 0.5, 1.0)]
    rows = [3.0, 1.0, 2.0, 2.0]
    sizes = [2.0, 1.0, 4.0, 1.0]
    reference = geoparquet._ideal_grid_boxes(spatial.bbox_union(boxes), len(boxes))
    uniform = [1.0] * len(reference)

    def sampled(
        layout: list[tuple[float, float, float, float]],
        weights: list[float],
        byte_shares: list[float],
    ) -> float:
        rng = random.Random(0)
        total_bytes = sum(byte_shares)
        read = 0.0
        draws = 40_000
        for _ in range(draws):
            (source,) = rng.choices(range(len(layout)), weights=weights)
            b = layout[source]
            cx = rng.uniform(b[0], b[2])
            cy = rng.uniform(b[1], b[3])
            half_w = geoparquet._QUERY_FRACTION * (b[2] - b[0]) / 2.0
            half_h = geoparquet._QUERY_FRACTION * (b[3] - b[1]) / 2.0
            window = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
            read += sum(
                byte_shares[j] / total_bytes
                for j, t in enumerate(layout)
                if t[0] <= window[2]
                and window[0] <= t[2]
                and t[1] <= window[3]
                and window[1] <= t[3]
            )
        return 1.0 - read / draws

    assert geoparquet._weighted_skip_rate(boxes, rows, sizes) == pytest.approx(
        sampled(boxes, rows, sizes), abs=0.01
    )
    assert geoparquet._weighted_skip_rate(reference, uniform, uniform) == pytest.approx(
        sampled(reference, uniform, uniform), abs=0.01
    )


def test_degenerate_layouts_have_a_defined_score() -> None:
    """Cross-check 5: no exception on a line, one box, or a window as big as the extent."""
    line = [(float(i), 2.0, float(i) + 1.0, 2.0) for i in range(8)]
    on_a_line = geoparquet._pruning_score(line)
    assert on_a_line.efficiency == 1.0  # the x axis alone judges it, and it tiles
    assert on_a_line.area_sum is None

    single = geoparquet._pruning_score([_UNIT])
    assert single.achievable == 0.0
    assert single.efficiency is None
    assert single.below_bar is None

    point_extent = geoparquet._pruning_score([(1.0, 1.0, 1.0, 1.0)] * 3)
    assert point_extent.achieved == 0.0
    assert point_extent.efficiency is None
    assert point_extent.below_bar is None


def test_efficiency_is_clipped_at_one() -> None:
    # Clustered data can beat the grid: eight tight boxes in a corner of the
    # extent skip more than eight tiles would.
    tight = [(0.1 * i, 0.0, 0.1 * i + 0.01, 0.01) for i in range(8)]
    score = geoparquet._pruning_score(tight)
    assert score.achieved > score.achievable
    assert score.efficiency == 1.0


def test_empty_layouts_have_nothing_to_skip() -> None:
    assert geoparquet._ideal_grid_boxes(_UNIT, 0) == []
    assert geoparquet._weighted_skip_rate([], [], []) == 0.0


def test_every_box_spanning_the_extent_scores_zero() -> None:
    """Cross-check 2: nothing to skip, whatever the count."""
    score = geoparquet._pruning_score([_UNIT] * 8)
    assert score.achieved == 0.0
    assert score.efficiency == 0.0
    assert score.area_sum == pytest.approx(8.0)


def test_overlapping_strips_prune_and_are_ordered() -> None:
    # Five overlapping vertical strips, each 35% of the extent: an x-sorted
    # layout. The old flat 30% area limit failed it, but a reader querying a
    # window can still skip most of the strips, which is what formats.md:38 now
    # measures. Ordering that prunes is ordering. Against the full-tiling
    # reference the strips sit close to the bar: skip 0.544 of 0.741 achievable.
    wide = [(1.625 * i, 0.0, 1.625 * i + 3.5, 10.0) for i in range(5)]
    score = geoparquet._pruning_score(wide)
    assert score.achieved == pytest.approx(0.583, abs=0.001)
    assert score.achievable == pytest.approx(0.777, abs=0.001)
    assert score.efficiency == pytest.approx(0.750, abs=0.001)
    assert not score.below_bar


@pytest.mark.parametrize("count", [2, 3, 5, 8, 13, 100])
def test_perfect_tiling_scores_exactly_one(count: int) -> None:
    """Cross-check 1: the reference layout is its own best case."""
    score = geoparquet._pruning_score(geoparquet._ideal_grid_boxes(_UNIT, count))
    assert score.efficiency == 1.0
    assert score.area_sum == pytest.approx(1.0)


def test_reference_tiling_covers_the_extent() -> None:
    """Cross-check 4: the reference is the full tiling, every cell filled.

    PORTO-FMT-053 pins one construction so every validator divides by the same
    number. The grid with an empty cell is a different number (0.644 against
    0.638 at n = 3 under the box-scaled window), and the last row of the
    pinned tiling stretches to the full width.
    """
    tiling = geoparquet._ideal_grid_boxes(_UNIT, 3)
    assert tiling == [(0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 1.0, 0.5), (0.0, 0.5, 1.0, 1.0)]
    assert sum(geoparquet._bbox_area(b) for b in tiling) == pytest.approx(1.0)
    uniform = [1.0, 1.0, 1.0]
    assert geoparquet._weighted_skip_rate(tiling, uniform, uniform) == pytest.approx(
        0.638, abs=0.001
    )
    old_grid = [(0.0, 0.0, 0.5, 0.5), (0.5, 0.0, 1.0, 0.5), (0.0, 0.5, 0.5, 1.0)]
    assert geoparquet._weighted_skip_rate(old_grid, uniform, uniform) == pytest.approx(
        0.644, abs=0.001
    )


@pytest.mark.parametrize("count", [1, 2, 5, 7, 8, 12, 100])
def test_reference_tiling_has_no_gap_at_any_count(count: int) -> None:
    tiling = geoparquet._ideal_grid_boxes(_UNIT, count)
    assert len(tiling) == count
    assert sum(geoparquet._bbox_area(b) for b in tiling) == pytest.approx(1.0)
    assert spatial.bbox_union(tiling) == _UNIT


@pytest.mark.parametrize("name", sorted(_VECTORS["layouts"]))
def test_shared_layout_vectors(name: str) -> None:
    """PORTO-FMT-054: the spec's abstract test vectors, vendored verbatim from
    specs/portolan/abstract-tests/spatial-metric-vectors.json."""
    vector = _VECTORS["layouts"][name]
    rows = [float(r) for r in vector["rows"]] if "rows" in vector else None
    sizes = [float(x) for x in vector["sizes"]] if "sizes" in vector else None
    score = geoparquet._pruning_score([tuple(b) for b in vector["boxes"]], rows, sizes)
    _assert_expected(score, vector["expected"])
    assert geoparquet._footer_verdict(score) == vector["expected"]["verdict"]


@pytest.mark.parametrize("name", sorted(_VECTORS["rows"]))
def test_shared_row_vectors(name: str) -> None:
    vector = _VECTORS["rows"][name]
    chunks, weights = geoparquet._chunked_bboxes(
        [tuple(b) if b is not None else None for b in vector["boxes"]]
    )
    assert [list(c) for c in chunks] == vector["expected_chunks"]
    assert weights == [float(w) for w in vector["chunk_rows"]]
    score = geoparquet._pruning_score(chunks, weights, weights)
    _assert_expected(score, vector["expected"])
    boxed = sum(1 for b in vector["boxes"] if b is not None)
    assert geoparquet._row_verdict(score, boxed) == vector["expected"]["verdict"]


def test_spatial_ordering_fails_below_the_bar() -> None:
    # Five boxes that each span the extent: nothing can be skipped, so the
    # achieved rate is 0% of what five row groups allow (formats.md:38).
    spanning = [(0.0, 0.0, 10.0, 10.0)] * 5
    score = geoparquet._pruning_score(spanning)
    assert score.efficiency == 0.0
    assert score.below_bar


def test_spatial_ordering_ignores_consecutive_overlap() -> None:
    # PORTO-FMT-049: a perfect grid tiling padded so neighbours touch. Every
    # consecutive pair overlaps, which the retired criterion faulted, but the
    # layout prunes as well as nine row groups allow.
    grid = geoparquet._ideal_grid_boxes((0.0, 0.0, 9.0, 9.0), 9)
    touching = [(a - 0.05, b - 0.05, c + 0.05, d + 0.05) for a, b, c, d in grid]
    pairs = len(touching) - 1
    overlaps = sum(geoparquet._bbox_overlaps(touching[i], touching[i + 1]) for i in range(pairs))
    # Well over the 30% the retired criterion allowed; not all 8, because the
    # pair that wraps from the end of one grid row to the start of the next are
    # diagonal neighbours rather than adjacent.
    assert overlaps / pairs > 0.30, "fixture must trip the retired overlap criterion"
    assert not geoparquet._pruning_score(touching).below_bar


def test_spatial_ordering_zero_extent_is_not_judged() -> None:
    # Both axes degenerate: every window hits every box, nothing could be
    # skipped by any layout, so the efficiency is undefined and there is no
    # verdict rather than a failure.
    score = geoparquet._pruning_score([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0)])
    assert score.efficiency is None
    assert score.area_sum is None
    assert score.below_bar is None  # no verdict: neither a failure nor a pass


@pytest.mark.parametrize(
    "roles,expected",
    [
        (["data"], False),
        (["source"], True),
        (["data", "source"], True),
        # Not a role: the Alternate Assets extension defines an `alternate`
        # field, and core.md uses the word as prose for a non-primary
        # representation. Neither exempts an asset from the format MUSTs.
        (["visual", "alternate"], False),
        (None, False),
        ("data", False),
    ],
)
def test_is_source(roles: object, expected: bool) -> None:
    assert is_source_asset({"roles": roles}) is expected


def test_bbox_helpers() -> None:
    assert geoparquet._bbox_area((0.0, 0.0, 2.0, 3.0)) == 6.0
    assert geoparquet._bbox_overlaps((0.0, 0.0, 2.0, 2.0), (1.0, 1.0, 3.0, 3.0))
    assert not geoparquet._bbox_overlaps((0.0, 0.0, 1.0, 1.0), (2.0, 2.0, 3.0, 3.0))
    assert spatial.bbox_union([(0.0, 1.0, 2.0, 3.0), (-1.0, 0.0, 1.0, 5.0)]) == (
        -1.0,
        0.0,
        2.0,
        5.0,
    )


# --- item-mirror fidelity ---------------------------------------------------
#
# PTL-DAT-016 compares each mirror row against the item it names. The geometry
# side builds both sides as shapely geometries, the datetime side reads RFC
# 3339, and both compare within a tolerance, so the encodings shapely is handed
# and the tolerance boundaries are exercised here rather than through a written
# file.


def _wkb_point(x: float, y: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, x, y)


def _wkb_ring(points: list[tuple[float, float]]) -> bytes:
    return struct.pack("<I", len(points)) + b"".join(struct.pack("<dd", x, y) for x, y in points)


def _wkb_polygon(rings: list[list[tuple[float, float]]]) -> bytes:
    return struct.pack("<BII", 1, 3, len(rings)) + b"".join(_wkb_ring(ring) for ring in rings)


_SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
_INNER = [(0.2, 0.2), (0.6, 0.2), (0.6, 0.6), (0.2, 0.2)]
# The same rings as JSON: parsed GeoJSON holds lists, never tuples.
_SQUARE_JSON = [[x, y] for x, y in _SQUARE]
_INNER_JSON = [[x, y] for x, y in _INNER]


def _mirror_item(**data: object) -> Node:
    return Node(
        path=PurePosixPath("c/i/i.json"),
        abs_path=Path("/nowhere/i.json"),
        kind="item",
        id="i",
        data=dict(data),
    )


class _FakeParquet:
    """Just the schema surface ``bbox_struct`` reads."""

    def __init__(self, *names: str) -> None:
        self.schema_arrow = type("_Schema", (), {"names": list(names), "metadata": None})()


class _UnreadableParquet(_FakeParquet):
    def read(self, columns: list[str]) -> object:
        raise OSError("truncated mid-column")


class _FakeTable:
    """Just the column surface ``bbox_column_values`` reads."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def column(self, _name: str) -> object:
        return type("_Column", (), {"to_pylist": lambda _self: self._rows})()


def _covering(bbox: object) -> dict[str, object]:
    return {"primary_column": "geometry", "columns": {"geometry": {"covering": {"bbox": bbox}}}}


def test_bbox_struct_reads_the_covering_declaration() -> None:
    geo = _covering({corner: ["bbox", corner] for corner in ("xmin", "ymin", "xmax", "ymax")})
    assert geoparquet.bbox_struct(_FakeParquet("bbox"), geo) == (
        "bbox",
        ["xmin", "ymin", "xmax", "ymax"],
    )


@pytest.mark.parametrize(
    "geo",
    [
        {},  # no geo metadata at all
        _covering(None),
        _covering({"xmin": ["bbox", "xmin"]}),  # an incomplete declaration
        _covering({c: ["box", "x", c] for c in ("xmin", "ymin", "xmax", "ymax")}),  # nested deeper
        _covering(
            {  # leaves under two different structs
                "xmin": ["west", "xmin"],
                "ymin": ["bbox", "ymin"],
                "xmax": ["bbox", "xmax"],
                "ymax": ["bbox", "ymax"],
            }
        ),
        _covering({c: ["absent", c] for c in ("xmin", "ymin", "xmax", "ymax")}),  # no such column
    ],
)
def test_unusable_covering_declaration_yields_no_bbox_column(geo: dict[str, object]) -> None:
    assert geoparquet.bbox_struct(_FakeParquet("bbox"), geo) is None


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"xmin": 4, "ymin": 50, "xmax": 6, "ymax": 52}, [4.0, 50.0, 6.0, 52.0]),
        ({"xmin": 4, "ymin": 50, "xmax": 6, "ymax": None}, None),
        (None, None),
    ],
)
def test_bbox_column_values(row: object, expected: object) -> None:
    table = _FakeTable([row])
    assert geoparquet.bbox_column_values(table, ("bbox", ["xmin", "ymin", "xmax", "ymax"])) == [
        expected
    ]


def test_mirror_rows_read_a_file_with_no_geo_metadata(tmp_path: Path) -> None:
    """No ``geo`` key names the primary column, so the plain name is used."""
    path = tmp_path / "items.parquet"
    pq.write_table(pa.table({"id": ["a"], "geometry": [_wkb_point(5.0, 51.0)]}), path)
    rows = mirror._read_mirror_rows(pq.ParquetFile(path))
    assert rows is not None
    assert rows.ids == ["a"]
    assert rows.geometry == [_wkb_point(5.0, 51.0)]
    assert rows.datetimes is None and rows.bboxes is None


def test_unreadable_mirror_columns_yield_no_rows() -> None:
    assert mirror._read_mirror_rows(_UnreadableParquet("id", "geometry")) is None


@pytest.mark.parametrize(
    "geometry,blob",
    [
        ({"type": "Point", "coordinates": [5.0, 51.0]}, _wkb_point(5.0, 51.0)),
        (  # big-endian
            {"type": "Point", "coordinates": [5.0, 51.0]},
            struct.pack(">BIdd", 0, 1, 5.0, 51.0),
        ),
        (  # ISO WKB Z: the third ordinate is not part of the comparison
            {"type": "Point", "coordinates": [5.0, 51.0]},
            struct.pack("<BIddd", 1, 1001, 5.0, 51.0, 7.0),
        ),
        (  # EWKB carries the SRID inline, ahead of the body
            {"type": "Point", "coordinates": [5.0, 51.0]},
            struct.pack("<BIIdd", 1, 0x20000001, 4326, 5.0, 51.0),
        ),
        (  # EWKB with the Z and M bits set
            {"type": "Point", "coordinates": [5.0, 51.0]},
            struct.pack("<BIdddd", 1, 1 | 0x80000000 | 0x40000000, 5.0, 51.0, 7.0, 9.0),
        ),
        (
            {"type": "LineString", "coordinates": [[0.0, 1.0], [2.0, 3.0]]},
            struct.pack("<BI", 1, 2) + _wkb_ring([(0.0, 1.0), (2.0, 3.0)]),
        ),
        (
            {"type": "Polygon", "coordinates": [_SQUARE_JSON, _INNER_JSON]},
            _wkb_polygon([_SQUARE, _INNER]),
        ),
        (
            {"type": "MultiPolygon", "coordinates": [[_SQUARE_JSON], [_INNER_JSON]]},
            struct.pack("<BII", 1, 6, 2) + _wkb_polygon([_SQUARE]) + _wkb_polygon([_INNER]),
        ),
        (
            {
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "Point", "coordinates": [1.0, 2.0]},
                    {"type": "Point", "coordinates": [3.0, 4.0]},
                ],
            },
            struct.pack("<BII", 1, 7, 2) + _wkb_point(1.0, 2.0) + _wkb_point(3.0, 4.0),
        ),
    ],
)
def test_wkb_encodings_agree_with_the_item_geometry(geometry: object, blob: bytes) -> None:
    """Every encoding a mirror may carry reaches shapely and matches the item."""
    assert mirror._geometry_agrees(_mirror_item(geometry=geometry), blob)


@pytest.mark.parametrize(
    "blob",
    [
        b"",  # nothing to read
        struct.pack("<BI", 1, 42),  # unknown geometry type
        struct.pack("<BIdd", 1, 1, 5.0, 51.0)[:-4],  # truncated coordinate
        struct.pack("<BII", 1, 2, 1_000_000),  # a vertex count the buffer cannot hold
        _wkb_polygon([_SQUARE[:-1]]),  # a ring that does not close
    ],
)
def test_unreadable_wkb_leaves_the_row_alone(blob: bytes) -> None:
    assert mirror._from_wkb(blob) is None
    item = _mirror_item(geometry={"type": "Point", "coordinates": [5.0, 51.0]})
    assert mirror._geometry_agrees(item, blob)


@pytest.mark.parametrize(
    "geometry,expected",
    [
        ({"type": "Point", "coordinates": [5.0, 51.0]}, "Point"),
        ({"type": "Polygon", "coordinates": [_SQUARE_JSON]}, "Polygon"),
        (
            {
                "type": "GeometryCollection",
                "geometries": [{"type": "Point", "coordinates": [1, 2]}],
            },
            "GeometryCollection",
        ),
    ],
)
def test_geojson_geometry_builds_a_shape(geometry: object, expected: str) -> None:
    built = mirror._shape(geometry)
    assert built is not None
    assert built.geom_type == expected


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        {"type": "Circle", "coordinates": [0, 0]},  # not a GeoJSON geometry type
        {"type": "Point", "coordinates": []},
        {"type": "Point", "coordinates": ["west", "north"]},
        {"type": "GeometryCollection", "geometries": {}},
        {"type": "GeometryCollection", "geometries": [{"type": "Circle"}]},
    ],
)
def test_unusable_geojson_geometry_is_none(geometry: object) -> None:
    assert mirror._shape(geometry) is None


def test_geometry_agreement_holds_within_the_tolerance() -> None:
    item = _mirror_item(geometry={"type": "Point", "coordinates": [5.0, 51.0]})
    assert mirror._geometry_agrees(item, _wkb_point(5.0 + 9e-7, 51.0))
    assert not mirror._geometry_agrees(item, _wkb_point(5.0 + 2e-6, 51.0))


@pytest.mark.parametrize(
    "blob,expected",
    [
        (None, False),  # a null row geometry against an item that has one
        (b"\x01\x2a\x00\x00\x00", True),  # an encoding this reader cannot decode
        ("POINT (5 51)", True),  # nor a column that is not binary
        (_wkb_polygon([_SQUARE]), False),  # a different geometry type
    ],
)
def test_geometry_agreement_on_unusable_rows(blob: object, expected: bool) -> None:
    item = _mirror_item(geometry={"type": "Point", "coordinates": [5.0, 51.0]})
    assert mirror._geometry_agrees(item, blob) is expected


def test_geometry_agreement_is_silent_without_an_item_geometry() -> None:
    assert mirror._geometry_agrees(_mirror_item(geometry=None), _wkb_point(0.0, 0.0))


def test_geometry_agreement_counts_vertices() -> None:
    item = _mirror_item(geometry={"type": "Polygon", "coordinates": [_SQUARE_JSON]})
    triangle = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0)]
    assert not mirror._geometry_agrees(item, _wkb_polygon([triangle]))


def test_geometry_agreement_reads_structure_not_only_vertices() -> None:
    """Two parts against one part with a hole, over the same vertices.

    A comparison that flattened both sides to a vertex list would call these
    equal: same type name, same points, same order. They are different
    footprints, and a mirror carrying one for an item declaring the other has
    drifted.
    """
    item = _mirror_item(
        geometry={"type": "MultiPolygon", "coordinates": [[_SQUARE_JSON], [_INNER_JSON]]}
    )
    one_part_with_a_hole = struct.pack("<BII", 1, 6, 1) + _wkb_polygon([_SQUARE, _INNER])
    assert not mirror._geometry_agrees(item, one_part_with_a_hole)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2024-01-01T00:00:00Z", datetime(2024, 1, 1, tzinfo=UTC)),
        ("2024-01-01t00:00:00z", datetime(2024, 1, 1, tzinfo=UTC)),
        ("2024-01-01 00:00:00", datetime(2024, 1, 1, tzinfo=UTC)),
        ("2024-01-01T02:00:00+0200", datetime(2024, 1, 1, tzinfo=UTC)),
        ("2024-01-01T00:00:00.123456789Z", datetime(2024, 1, 1, 0, 0, 0, 123456, UTC)),
        ("2024-01-01", None),
        ("not a timestamp", None),
        ("2024-13-01T00:00:00Z", None),
    ],
)
def test_timestamp_parsing(value: str, expected: datetime | None) -> None:
    assert mirror._parse_timestamp(value) == expected


def test_datetime_agreement_holds_within_the_tolerance() -> None:
    item = _mirror_item(properties={"datetime": "2024-01-01T00:00:00Z"})
    assert mirror._datetime_agrees(item, datetime(2024, 1, 1, 0, 0, 1, tzinfo=UTC))
    assert not mirror._datetime_agrees(item, datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC))


def test_datetime_agreement_normalizes_the_zone() -> None:
    item = _mirror_item(properties={"datetime": "2024-01-01T00:00:00Z"})
    assert mirror._datetime_agrees(item, datetime(2024, 1, 1))  # naive column reads as UTC
    assert mirror._datetime_agrees(item, "2024-01-01T01:00:00+01:00")


@pytest.mark.parametrize("value", [None, "yesterday", 1704067200])
def test_datetime_agreement_fails_on_an_unusable_row(value: object) -> None:
    item = _mirror_item(properties={"datetime": "2024-01-01T00:00:00Z"})
    assert not mirror._datetime_agrees(item, value)


@pytest.mark.parametrize("properties", [None, {}, {"datetime": None}, {"datetime": 5}])
def test_datetime_agreement_is_silent_without_an_item_datetime(properties: object) -> None:
    assert mirror._datetime_agrees(_mirror_item(properties=properties), None)


def test_bbox_agreement() -> None:
    item = _mirror_item(bbox=[4.0, 50.0, 6.0, 52.0])
    assert mirror._bbox_agrees(item, [4.0, 50.0, 6.0, 52.0 + 5e-7])
    assert not mirror._bbox_agrees(item, [4.0, 50.0, 6.0, 52.5])
    assert not mirror._bbox_agrees(item, None)
    assert mirror._bbox_agrees(_mirror_item(), None)


def test_bbox_agreement_drops_the_z_ordinates() -> None:
    item = _mirror_item(bbox=[4.0, 50.0, 0.0, 6.0, 52.0, 100.0])
    assert mirror._bbox_agrees(item, [4.0, 50.0, 6.0, 52.0])
