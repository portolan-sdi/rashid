"""Generators for real GeoParquet/COG asset bytes used by the data-pass tests.

Not a test module (no ``test_`` prefix, so pytest does not collect it); imported
only after callers ``importorskip`` the geospatial stack. Produces spec-compliant
assets plus deliberately non-compliant variants for each ``PTL-DAT`` storage rule,
with checksums computed from the bytes so nothing is committed and nothing drifts.
"""

from __future__ import annotations

import hashlib
import json
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
