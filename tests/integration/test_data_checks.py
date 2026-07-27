"""Byte-check logic — fake readers with in-memory bytes, no network.

Needs the ``reis[data]`` extra (importing :mod:`reis.data.checks` pulls the
geospatial stack), so it lives under integration and skips when the extra is
absent. Covers the checksum, size, and format checks with synthetic bytes plus
the PMTiles header parse and the reprojection helper; the parquet-geo and COG
checks are exercised end-to-end in ``test_data_catalog``.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("pyproj")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
from pyproj import CRS, Transformer  # noqa: E402

import reis.data.checks as checks  # noqa: E402
from reis.catalog import Node  # noqa: E402
from reis.data import (  # noqa: E402
    DAT_CHECKSUM,
    DAT_FORMAT,
    DAT_SIZE,
)
from reis.data.reader import Locator  # noqa: E402
from reis.model import Severity  # noqa: E402

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
    # 0x18 = keccak-256, valid multihash code reis cannot compute.
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

    geo = checks._geo_from_pmtiles(Locator(is_remote=False, source=str(path)))

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


def _rd_new_geo() -> checks._Geo:
    return checks._Geo(bbox=list(_RD_NEW_BBOX), epsg=28992, crs=CRS.from_epsg(28992))


def test_wgs84_bounds_of_unprojected_bbox_are_exact() -> None:
    bbox = [4.0, 50.0, 6.0, 52.0]
    geo = checks._Geo(bbox=bbox, epsg=4326, crs=CRS.from_epsg(4326))

    bounds = checks._wgs84_bounds(geo)

    assert bounds is not None
    assert not bounds.reprojected
    assert bounds.outer == pytest.approx(bbox)
    assert bounds.inner == pytest.approx(bbox)


def test_wgs84_bounds_reproject_a_projected_bbox() -> None:
    to_mercator = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(3857), always_xy=True)
    minx, miny = to_mercator.transform(4.0, 50.0)
    maxx, maxy = to_mercator.transform(6.0, 52.0)
    geo = checks._Geo(bbox=[minx, miny, maxx, maxy], epsg=3857, crs=CRS.from_epsg(3857))

    bounds = checks._wgs84_bounds(geo)

    assert bounds is not None
    assert bounds.reprojected
    # web mercator keeps meridians and parallels straight, so both bounds close
    # in on the same box from either side
    assert bounds.outer == pytest.approx([4.0, 50.0, 6.0, 52.0], abs=1e-6)
    assert bounds.inner == pytest.approx([4.0, 50.0, 6.0, 52.0], abs=1e-6)


def test_wgs84_bounds_bracket_the_true_envelope() -> None:
    bounds = checks._wgs84_bounds(_rd_new_geo())

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
    bounds = checks._wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert checks._bbox_within(_RD_NEW_ENVELOPE, bounds)


@pytest.mark.parametrize(
    "over",
    [
        [3.0, 50.75, 7.22, 53.57],  # west of any longitude the data can reach
        [3.31, 50.5, 7.22, 53.9],  # south and north of any latitude it can reach
    ],
)
def test_projected_bbox_rejects_an_over_claimed_declaration(over: list[float]) -> None:
    bounds = checks._wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert not checks._bbox_within(over, bounds)


def test_projected_mismatch_names_both_bounds() -> None:
    bounds = checks._wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    assert bounds.inner is not None
    message = checks._bbox_mismatch_message("data", [4.0, 51.0, 6.0, 53.0], bounds, 28992)

    assert "EPSG:28992" in message
    assert checks._fmt_bbox(bounds.inner) in message
    assert checks._fmt_bbox(bounds.outer) in message


def test_projected_bbox_rejects_an_under_claimed_declaration() -> None:
    """A stale bbox covering part of the data must still warn."""
    bounds = checks._wgs84_bounds(_rd_new_geo())

    assert bounds is not None
    under = [4.0, 51.0, 6.0, 53.0]
    assert not checks._bbox_within(under, bounds)


@pytest.mark.parametrize("epsg", [4326, 28992])
def test_wgs84_bounds_of_an_untight_bbox_have_no_inner_side(epsg: int) -> None:
    """A raster grid only contains its data, so no side is pinned from inside."""
    bbox = [4.0, 50.0, 6.0, 52.0] if epsg == 4326 else list(_RD_NEW_BBOX)
    geo = checks._Geo(bbox=bbox, epsg=epsg, crs=CRS.from_epsg(epsg), tight=False)

    bounds = checks._wgs84_bounds(geo)

    assert bounds is not None
    assert bounds.inner is None


def test_untight_bbox_accepts_a_declaration_inside_the_collar() -> None:
    """A nodata collar puts the real footprint well inside the grid extent."""
    grid = checks._Geo(bbox=[4.0, 50.0, 6.0, 52.0], epsg=4326, crs=CRS.from_epsg(4326), tight=False)
    bounds = checks._wgs84_bounds(grid)

    assert bounds is not None
    assert checks._bbox_within([4.8, 50.9, 5.1, 51.2], bounds)
    assert not checks._bbox_within([3.5, 50.9, 5.1, 51.2], bounds)


def test_untight_mismatch_names_the_containing_extent() -> None:
    bounds = checks._Wgs84Bounds(outer=[4.0, 50.0, 6.0, 52.0], inner=None)

    message = checks._bbox_mismatch_message("data", [3.5, 50.9, 5.1, 51.2], bounds, 4326)

    assert "is not contained by the asset's extent" in message


def test_wgs84_bounds_skip_an_antimeridian_crossing() -> None:
    """Straddling the seam, min/max in degrees describes no box at all."""
    # PDC Mercator, central meridian 150E; the seam sits near x=3.34e6.
    geo = checks._Geo(bbox=[3.0e6, -1.0e6, 5.0e6, 1.0e6], epsg=3832, crs=CRS.from_epsg(3832))

    assert checks._wgs84_bounds(geo) is None


@pytest.mark.parametrize("epsg", [3035, 32631])
def test_wgs84_bounds_skip_an_unprojectable_bbox(epsg: int) -> None:
    """Coordinates outside the projection's domain reproject to infinity."""
    geo = checks._Geo(bbox=[-1e9, -1e9, 1e9, 1e9], epsg=epsg, crs=CRS.from_epsg(epsg))

    assert checks._wgs84_bounds(geo) is None


def test_consistency_skips_the_bbox_it_cannot_reproject(monkeypatch: pytest.MonkeyPatch) -> None:
    geo = checks._Geo(bbox=[3.0e6, -1.0e6, 5.0e6, 1.0e6], epsg=3832, crs=CRS.from_epsg(3832))
    monkeypatch.setattr(checks, "_extract_geo", lambda expected, located: geo)
    located = Locator(is_remote=False, source="unused")

    defects = checks._check_consistency(_item(_asset()), "data", _asset(), "parquet", located)

    assert defects == []


def test_bbox_within_tolerance() -> None:
    exact = checks._Wgs84Bounds(outer=[4.0, 50.0, 6.0, 52.0], inner=[4.0, 50.0, 6.0, 52.0])
    assert checks._bbox_within([4.005, 50.0, 6.0, 52.0], exact)
    assert not checks._bbox_within([4.5, 50.0, 6.0, 52.0], exact)


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
    assert checks._detect_format(head) == expected


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
    assert checks._expected_format(media) == expected


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
    assert checks._as_bbox(raw) == out


def test_declared_bbox_collection_extent() -> None:
    node = Node(
        path=PurePosixPath("c/collection.json"),
        abs_path=Path("/x"),
        kind="collection",
        id="c",
        data={"extent": {"spatial": {"bbox": [[4.0, 50.0, 6.0, 52.0]]}}},
    )
    assert checks._declared_bbox(node) == [4.0, 50.0, 6.0, 52.0]


def test_declared_epsg_from_properties() -> None:
    node = Node(
        path=PurePosixPath("c/i/i.json"),
        abs_path=Path("/x"),
        kind="item",
        id="i",
        data={"properties": {"proj:epsg": 3857}},
    )
    assert checks._declared_epsg(node, {}) == 3857
    assert checks._declared_epsg(node, {"proj:epsg": 32631}) == 32631  # asset wins


def test_declared_epsg_absent() -> None:
    node = Node(path=PurePosixPath("c/i/i.json"), abs_path=Path("/x"), kind="item", id="i", data={})
    assert checks._declared_epsg(node, {}) is None


def test_check_raster_reader_error_is_info() -> None:
    located = Locator(is_remote=False, source="/no/such/file.tif")
    defects = checks._check_raster("data", located)
    assert [d.rule_id for d in defects] == [checks.DAT_COG]
    assert defects[0].severity is Severity.INFO


def test_geo_from_parquet_without_geo_metadata(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"value": [1, 2, 3]}), path)
    assert checks._geo_from_parquet(Locator(is_remote=False, source=str(path))) is None


def test_consistency_unreadable_is_info() -> None:
    node = _item(_asset())
    located = Locator(is_remote=False, source="/no/such/file.parquet")
    defects = checks._check_consistency(node, "data", _asset(), "parquet", located)
    assert [d.rule_id for d in defects] == [checks.DAT_CONSISTENCY]
    assert defects[0].severity is Severity.INFO


def test_spatial_ordering_single_or_empty_group() -> None:
    assert checks._is_spatially_ordered([(0.0, 0.0, 1.0, 1.0)])
    assert checks._is_spatially_ordered([])


def test_spatial_ordering_low_overlap() -> None:
    disjoint = [(0.0, 0.0, 1.0, 1.0), (2.0, 2.0, 3.0, 3.0), (4.0, 4.0, 5.0, 5.0)]
    assert checks._is_spatially_ordered(disjoint)


def test_spatial_ordering_high_overlap_fails() -> None:
    piled = [(0.0, 0.0, 5.0, 5.0)] * 4
    assert not checks._is_spatially_ordered(piled)


def test_spatial_ordering_high_locality_despite_overlap() -> None:
    # Every consecutive pair overlaps (low-overlap fails), but each box is a small
    # fraction of the extent, so the locality criterion carries the ordering.
    boxes = [(float(i), 0.0, float(i) + 2.0, 1.0) for i in range(10)]
    assert not all(  # sanity: neighbours really do overlap
        not checks._bbox_overlaps(boxes[i], boxes[i + 1]) for i in range(len(boxes) - 1)
    )
    assert checks._is_spatially_ordered(boxes)


def test_spatial_ordering_zero_extent_is_ordered() -> None:
    assert checks._is_spatially_ordered([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0)])


@pytest.mark.parametrize(
    "roles,expected",
    [
        (["data"], False),
        (["data", "source"], True),
        (["visual", "alternate"], True),
        (None, False),
        ("data", False),
    ],
)
def test_is_alternate(roles: object, expected: bool) -> None:
    assert checks._is_alternate({"roles": roles}) is expected


def test_bbox_helpers() -> None:
    assert checks._bbox_area((0.0, 0.0, 2.0, 3.0)) == 6.0
    assert checks._bbox_overlaps((0.0, 0.0, 2.0, 2.0), (1.0, 1.0, 3.0, 3.0))
    assert not checks._bbox_overlaps((0.0, 0.0, 1.0, 1.0), (2.0, 2.0, 3.0, 3.0))
    assert checks._bbox_union([(0.0, 1.0, 2.0, 3.0), (-1.0, 0.0, 1.0, 5.0)]) == (
        -1.0,
        0.0,
        2.0,
        5.0,
    )
