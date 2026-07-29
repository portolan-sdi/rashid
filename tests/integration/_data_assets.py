"""Generators for real GeoParquet/COG asset bytes used by the data-pass tests.

Not a test module (no ``test_`` prefix, so pytest does not collect it); imported
only after callers ``importorskip`` the geospatial stack. Produces spec-compliant
assets plus deliberately non-compliant variants for each ``PTL-DAT`` storage rule,
with checksums computed from the bytes so nothing is committed and nothing drifts.
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from pyproj import CRS
from rasterio.transform import from_bounds

BBOX = [4.0, 50.0, 6.0, 52.0]


def multihash(payload: bytes) -> str:
    return "1220" + hashlib.sha256(payload).hexdigest()


def ordered_points(n: int = 6) -> list[tuple[float, float]]:
    """Points on the bbox diagonal, ascending — nearby rows stay nearby."""
    minx, miny, maxx, maxy = BBOX
    return [
        (minx + (maxx - minx) * i / (n - 1), miny + (maxy - miny) * i / (n - 1)) for i in range(n)
    ]


def interleaved_points() -> list[tuple[float, float]]:
    """Each consecutive pair spans the whole extent, so row groups overlap."""
    minx, miny, maxx, maxy = BBOX
    return [
        (minx, miny),
        (maxx, maxy),
        (minx + 0.2, miny + 0.2),
        (maxx - 0.2, maxy - 0.2),
        (minx + 0.4, miny + 0.4),
        (maxx - 0.4, maxy - 0.4),
    ]


def write_geoparquet(
    path: Path,
    *,
    points: list[tuple[float, float]] | None = None,
    covering: bool = True,
    row_group_size: int = 2,
    geo: bool = True,
    version: str = "1.1.0",
    columns: dict[str, list[object]] | None = None,
) -> None:
    pts = points if points is not None else ordered_points()
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    wkb = [struct.pack("<BIdd", 1, 1, x, y) for x, y in pts]
    cols: dict[str, object] = {
        "geometry": pa.array(wkb, type=pa.binary()),
        "value": list(range(len(pts))),
    }
    if columns:
        cols.update(columns)
    meta: dict[str, object] = {
        "version": version,
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Point"],
                "crs": json.loads(CRS.from_epsg(4326).to_json()),
                "bbox": [min(xs), min(ys), max(xs), max(ys)],
            }
        },
    }
    if covering:
        cols["bbox"] = pa.StructArray.from_arrays(
            [
                pa.array(xs, pa.float64()),
                pa.array(ys, pa.float64()),
                pa.array(xs, pa.float64()),
                pa.array(ys, pa.float64()),
            ],
            names=["xmin", "ymin", "xmax", "ymax"],
        )
        meta["columns"]["geometry"]["covering"] = {  # type: ignore[index]
            "bbox": {
                "xmin": ["bbox", "xmin"],
                "ymin": ["bbox", "ymin"],
                "xmax": ["bbox", "xmax"],
                "ymax": ["bbox", "ymax"],
            }
        }
    table = pa.table(cols)
    if geo:
        table = table.replace_schema_metadata({b"geo": json.dumps(meta).encode()})
    pq.write_table(table, path, row_group_size=row_group_size)


def write_item_mirror(
    path: Path,
    ids: list[str] | None,
    *,
    covering: bool = True,
    row_group_size: int = 2,
    ordered: bool = True,
) -> None:
    """A stac-geoparquet item mirror: one row per item, keyed by item id.

    Conformant by default, because the GeoParquet storage rules bind a mirror
    as they bind vector data (PORTO-FMT-043): rows on the bbox diagonal, a
    covering column, row groups under the ceiling. ``ordered=False`` and
    ``covering=False`` produce the shapes PTL-DAT-006 and PTL-DAT-007 reject.
    ``ids=None`` omits the ``id`` column, which is the shape PTL-DAT-016
    rejects outright.
    """
    count = len(ids) if ids else 1
    source = ordered_points(max(count, 2)) if ordered else interleaved_points()
    pts = source[:count]
    columns = {"id": pa.array(ids, type=pa.string())} if ids is not None else None
    write_geoparquet(
        path,
        points=pts,
        covering=covering,
        row_group_size=row_group_size,
        columns=columns,
    )


# --- mirrors from the reference writer --------------------------------------
#
# The generators above hand-build Parquet, which is fine for pinning one shape
# but cannot show what a producer actually publishes. These drive
# ``stac_geoparquet``'s own parser and writer, the path portolan-cli takes, so
# the tests see the file shapes real catalogs carry (#66).

_HILBERT_ORDER = 16


def _hilbert_distance(x: int, y: int, order: int = _HILBERT_ORDER) -> int:
    """Position of integer cell ``(x, y)`` along a Hilbert curve of ``order``."""
    distance = 0
    side = 1 << (order - 1)
    while side > 0:
        rx = 1 if x & side else 0
        ry = 1 if y & side else 0
        distance += side * side * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = side - 1 - x
                y = side - 1 - y
            x, y = y, x
        side //= 2
    return distance


def _hilbert_key(point: tuple[float, float]) -> int:
    cells = (1 << _HILBERT_ORDER) - 1
    x, y = point
    return _hilbert_distance(int((x + 180.0) / 360.0 * cells), int((y + 90.0) / 180.0 * cells))


def scattered_items(count: int, *, seed: int = 1, ordered: bool = False) -> list[dict[str, object]]:
    """STAC item dicts at pseudo-random points across the globe.

    ``ordered=True`` sorts them along a Hilbert curve, which is the spatial
    ordering formats.md:30 asks for; left unordered they are the scattered
    mirror the issue reports as passing clean.
    """
    rng = random.Random(seed)
    points = [(rng.uniform(-180.0, 180.0), rng.uniform(-80.0, 80.0)) for _ in range(count)]
    if ordered:
        points.sort(key=_hilbert_key)
    return [
        {
            "type": "Feature",
            "stac_version": "1.1.0",
            "id": f"item-{i:07d}",
            "geometry": {"type": "Point", "coordinates": [x, y]},
            "bbox": [x, y, x, y],
            "properties": {"datetime": "2024-01-01T00:00:00Z"},
            "links": [],
            "assets": {"data": {"href": f"https://example.org/{i}.tif", "type": "image/tiff"}},
            "collection": "scattered",
        }
        for i, (x, y) in enumerate(points)
    ]


def write_stac_geoparquet(
    path: Path, items: list[dict[str, object]], *, chunk_size: int | None = None
) -> None:
    """Write ``items`` with ``stac_geoparquet``, as a producer does.

    ``chunk_size=None`` is the plain default: ``parse_stac_items_to_arrow``
    infers the schema from the whole input and returns one contiguous record
    batch, so ``to_parquet`` writes one row group whatever the item count.
    Passing a chunk size selects the streaming schema path, which batches the
    input and writes one row group per batch.
    """
    from stac_geoparquet.arrow import parse_stac_items_to_arrow, to_parquet

    if chunk_size is None:
        reader = parse_stac_items_to_arrow(items)
    else:
        reader = parse_stac_items_to_arrow(items, chunk_size=chunk_size, schema="FirstBatch")
    to_parquet(reader, path)


def write_cog(
    path: Path,
    *,
    stats: bool = True,
    valid_percent: bool = True,
    nodata: float | None = None,
    overviews: bool = True,
    size: int = 1024,
    blocksize: int | None = None,
) -> None:
    """A valid COG (COG driver) with, by default, embedded per-band statistics.

    ``overviews=False`` sets the COG driver's ``OVERVIEWS=NONE``: the file stays
    a structurally valid COG (cog_validate passes it with only a warning) but
    carries no internal overviews. ``blocksize`` overrides the internal tile
    size (default: 512, clamped to the image).
    """
    arr = (np.arange(size * size, dtype="uint8") % 251).reshape(1, size, size)
    with rasterio.open(
        path,
        "w",
        driver="COG",
        height=size,
        width=size,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_bounds(*BBOX, size, size),
        compress="deflate",
        blocksize=blocksize if blocksize is not None else min(512, size),
        nodata=nodata,
        overviews="AUTO" if overviews else "NONE",
    ) as dst:
        dst.write(arr)
        if stats:
            band = arr[0]
            tags = {
                "STATISTICS_MINIMUM": str(float(band.min())),
                "STATISTICS_MAXIMUM": str(float(band.max())),
                "STATISTICS_MEAN": str(float(band.mean())),
                "STATISTICS_STDDEV": str(float(band.std())),
            }
            if valid_percent:
                tags["STATISTICS_VALID_PERCENT"] = "100"
            dst.update_tags(1, **tags)


def write_tiled_tiff(path: Path, *, size: int = 1024, blockx: int = 512, blocky: int = 256) -> None:
    """A tiled GeoTIFF with (by default, non-square) internal tiles.

    Uses the plain GTiff driver: the COG driver only writes square tiles, and
    the tile-size check inspects tiling regardless of COG validity.
    """
    arr = (np.arange(size * size, dtype="uint8") % 251).reshape(1, size, size)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_bounds(*BBOX, size, size),
        tiled=True,
        blockxsize=blockx,
        blockysize=blocky,
    ) as dst:
        dst.write(arr)


def write_plain_tiff(path: Path, *, size: int = 1024) -> None:
    """A striped GeoTIFF above 512px — a real TIFF that is not a COG."""
    arr = (np.arange(size * size, dtype="uint8") % 251).reshape(1, size, size)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_bounds(*BBOX, size, size),
    ) as dst:
        dst.write(arr)
