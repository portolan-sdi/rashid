"""Byte-level checks: does an asset's data match what its metadata declares?

Reached only through :func:`reis.data.default_validator`, so importing this
module (and the geospatial stack it pulls) happens only when the opt-in data
pass actually runs. Each check turns a divergence between the declared metadata
and the real bytes into a :class:`reis.data.DataDefect`:

- ``PTL-DAT-001`` recomputed multihash ≠ ``file:checksum`` (MUST)
- ``PTL-DAT-002`` byte length ≠ ``file:size`` (MUST)
- ``PTL-DAT-003`` magic bytes ≠ declared media type (MUST)
- ``PTL-DAT-004`` a raster asset is not a valid COG (MUST, formats.md:91)
- ``PTL-DAT-005`` actual bbox/CRS inconsistent with the declared metadata (advisory)
- ``PTL-DAT-006`` GeoParquet rows are not spatially ordered (MUST, formats.md:30)
- ``PTL-DAT-007`` no per-row-group spatial statistics (MUST, formats.md:39)
- ``PTL-DAT-008`` a row group exceeds 150,000 rows (MUST, formats.md:50)
- ``PTL-DAT-009`` COG bands lack embedded statistics (MUST, formats.md:95)
- ``PTL-DAT-010`` a band lacks embedded valid percent (SHOULD; MUST — and thus
  an ERROR — when the band has a nodata value, formats.md:121)
- ``PTL-DAT-011`` a raster larger than its own internal tile has no internal
  overviews. OGC 21-026 makes overviews optional in base COG but a SHALL in
  its Optimized GeoTIFF conformance class (/req/optimized_geotiff) — the class
  Portolan's efficient-range-request mandate targets. ``cog_validate`` checks
  base COG, so it accepts such a file with only a warning; without overviews a
  zoomed-out render reads every full-resolution byte.
- ``PTL-DAT-012`` GeoParquet version is not 1.1 or 2.x (MUST, formats.md:25)
- ``PTL-DAT-013`` internal tiles are not square or exceed 512px (MUST,
  OGC 21-026 /req/optimized_geotiff/small-sizes via formats.md:121)
- ``PTL-DAT-014`` partition files diverge in Parquet schema (MUST,
  formats.md:91: "validated by tooling reading file footers")
- ``PTL-DAT-015`` a tabular (plain Parquet) collection is missing the SHOULDs
  of formats.md, Tabular Data: ``table:columns`` documenting the columns, and
  ``extent.temporal`` when the file carries a temporal column (WARNING)
- ``PTL-DAT-016`` an item rollup's ids diverge from the collection's items
  (MUST, formats.md, Raster § Item rollup). Rollups are routed here instead of
  through the GeoParquet checks, which bind data assets only.

``PTL-DAT-007`` also carries formats.md's covering-column recommendation: a
GeoParquet 2.x file that satisfies the statistics MUST through native
``GeospatialStatistics`` alone, without the RECOMMENDED ``bbox`` covering
column, gets a WARNING rather than an ERROR.

The ``STATISTICS_APPROXIMATE`` MUST-when-estimated cannot be checked from the
bytes: whether the statistics were estimated is not knowable after the fact.

Checks that cannot run for a given asset — bytes unreachable, an unsupported
hash function, an unreadable header — degrade to an INFO or to silence rather
than a false ERROR; a present-but-unverifiable checksum is not a conformance
failure the way a wrong one is.
"""

from __future__ import annotations

import glob as globmodule
import hashlib
import json
import math
import posixpath
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from pyproj import CRS, Transformer
from rio_cogeo.cogeo import cog_validate

from reis._multihash import decode_multihash
from reis.catalog import CatalogGraph, Node
from reis.data import (
    DAT_CHECKSUM,
    DAT_COG,
    DAT_COG_STATS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_GEOPARQUET_VERSION,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROLLUP,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SIZE,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
    DataDefect,
)
from reis.data.reader import AssetReader, Locator
from reis.model import Severity
from reis.rules.rollup import is_rollup_asset

# Multihash function code -> hashlib algorithm name.
_HASH_ALGOS = {
    0x11: "sha1",
    0x12: "sha256",
    0x13: "sha512",
    0x14: "sha3_512",
    0x15: "sha3_384",
    0x16: "sha3_256",
    0x17: "sha3_224",
}

# Recognized formats: (magic-byte probe, media-type marker).
_PMTILES_MAGIC = b"PMTiles"
_HEAD_BYTES = 16  # enough for every magic-number probe below

# bbox comparison tolerance in degrees (~1 km); absorbs rounding and
# reprojection gridding so only genuine divergence trips PTL-DAT-005.
_BBOX_TOL = 0.01

# Intermediate points per bbox edge when reprojecting. A projected rectangle's
# edges curve in WGS84, and their extremes usually fall between the corners, so
# every edge is walked rather than sampled at its ends.
_DENSIFY_PTS = 21

# formats.md:50 — a GeoParquet row group MUST hold no more than this many rows.
_MAX_ROW_GROUP_ROWS = 150_000

# formats.md:30 — spatial ordering passes on either criterion.
_MAX_OVERLAP_FRACTION = 0.30  # < 30% of consecutive row-group pairs may overlap
_MAX_LOCALITY_RATIO = 0.25  # row-group boxes average < 25% of the file extent

# geotiff-stats-headers.md — the embedded per-band statistics a COG MUST carry.
_COG_STAT_KEYS = (
    "STATISTICS_MINIMUM",
    "STATISTICS_MAXIMUM",
    "STATISTICS_MEAN",
    "STATISTICS_STDDEV",
)

# formats.md:121 — SHOULD per band, MUST when the band has a nodata value.
_VALID_PERCENT_KEY = "STATISTICS_VALID_PERCENT"


@dataclass(frozen=True)
class _Geo:
    """Actual spatial metadata read from an asset's bytes."""

    bbox: list[float] | None
    epsg: int | None
    crs: Any | None  # pyproj CRS, when EPSG alone doesn't capture it
    # Whether ``bbox`` is the tight envelope of the data or merely contains it.
    # A geometry column's bbox is tight, so the data reaches every edge; a raster
    # grid only contains its data, and a nodata collar can be wide.
    tight: bool = True


@dataclass(frozen=True)
class _Wgs84Bounds:
    """Where the WGS84 envelope of an asset's data must lie.

    ``outer`` contains that envelope. ``inner``, when known, is contained by it,
    so together they pin each side from opposite directions. A bracket is the
    most a header-only check can say about a projected asset: under a non-affine
    projection the native rectangle maps to a curved quadrilateral, whose
    bounding box is a strict superset of the envelope of the data inside it, so
    neither box alone is the answer. An unprojected asset has no distortion to
    absorb and the two boxes coincide.

    ``inner`` is ``None`` when the native bbox was not tight to begin with, which
    leaves plain containment in ``outer`` as the only sound assertion.
    """

    outer: list[float]
    inner: list[float] | None
    reprojected: bool = False


def check_node(
    node: Node, reader: AssetReader, graph: CatalogGraph | None = None
) -> list[DataDefect]:
    """Verify every asset on ``node`` against its declared metadata.

    ``graph`` is optional because one check, the item rollup's agreement with
    the collection's items, needs the object's children. Without it that check
    is skipped.
    """
    defects: list[DataDefect] = []
    defects.extend(_check_partition_schemas(node))
    for key, asset in _assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            continue  # PTL-AST-001 reports a missing href
        defects.extend(_check_asset(node, key, asset, href, reader, graph))
    return defects


def _assets_of(node: Node) -> list[tuple[str, dict[str, Any]]]:
    assets = node.data.get("assets")
    if not isinstance(assets, dict):
        return []
    return [(key, asset) for key, asset in assets.items() if isinstance(asset, dict)]


def _check_asset(
    node: Node,
    key: str,
    asset: dict[str, Any],
    href: str,
    reader: AssetReader,
    graph: CatalogGraph | None = None,
) -> list[DataDefect]:
    defects: list[DataDefect] = []
    media_type = asset.get("type")
    expected = _expected_format(media_type) if isinstance(media_type, str) else None

    defects.extend(_check_bytes(key, asset, href, expected, node, reader))

    located = reader.locate(node, href)
    if located is None or _is_alternate(asset):
        # A source/alternate original (a non-cloud-native representation kept
        # alongside the primary) is exempt from the cloud-native format MUSTs;
        # its bytes are still checksum/size/format-verified above.
        return defects
    if is_rollup_asset(asset):
        # formats.md, Raster § Item rollup: the GeoParquet requirements bind
        # data assets, and a validator MUST NOT hold a rollup to them
        # (PORTO-FMT-043). An item index is not queried by extent, so spatial
        # ordering, row-group statistics, and the row-group ceiling do not
        # apply, and neither does the tabular pair of SHOULDs. What does apply
        # is agreement with the items it indexes.
        defects.extend(_check_rollup(node, key, located, graph))
        return defects
    if expected == "tiff":
        defects.extend(_check_raster(key, located))
    if expected == "parquet":
        defects.extend(_check_geoparquet(key, located))
        defects.extend(_check_tabular(node, key, asset, located))
    if expected in {"parquet", "tiff", "pmtiles"}:
        defects.extend(_check_consistency(node, key, asset, expected, located))
    return defects


def _is_alternate(asset: dict[str, Any]) -> bool:
    roles = asset.get("roles")
    if not isinstance(roles, list):
        return False
    return any(isinstance(role, str) and role in ("source", "alternate") for role in roles)


def _check_bytes(
    key: str,
    asset: dict[str, Any],
    href: str,
    expected: str | None,
    node: Node,
    reader: AssetReader,
) -> list[DataDefect]:
    """Stream the object once: verify checksum, size, and format magic."""
    stream = reader.stream(node, href)
    if stream is None:
        return []  # not fetchable; metadata pass owns missing/foreign hrefs

    declared_checksum = asset.get("file:checksum")
    decoded = decode_multihash(declared_checksum)
    algo: str | None = None
    hasher: Any = None
    if decoded is not None:
        code, digest = decoded
        algo = _HASH_ALGOS.get(code)
        if algo is not None:
            hasher = hashlib.new(algo)

    head = b""
    count = 0
    try:
        for chunk in stream:
            count += len(chunk)
            if len(head) < _HEAD_BYTES:
                head += chunk[: _HEAD_BYTES - len(head)]
            if hasher is not None:
                hasher.update(chunk)
    except OSError as exc:
        return [
            DataDefect(
                DAT_CHECKSUM,
                Severity.INFO,
                f"asset '{key}' bytes could not be read ({exc}); not verified",
                key,
            )
        ]

    defects: list[DataDefect] = []
    defects.extend(_verify_checksum(key, decoded, algo, hasher))
    defects.extend(_verify_size(key, asset.get("file:size"), count))
    defects.extend(_verify_format(key, expected, head))
    return defects


def _verify_checksum(
    key: str,
    decoded: tuple[int, bytes] | None,
    algo: str | None,
    hasher: Any,
) -> list[DataDefect]:
    if decoded is None:
        return []  # absent or malformed: PTL-AST-003/004 own that
    if algo is None:
        code = decoded[0]
        return [
            DataDefect(
                DAT_CHECKSUM,
                Severity.INFO,
                f"asset '{key}' file:checksum uses hash function 0x{code:x}, "
                "which reis cannot compute; not verified",
                key,
                "file:checksum",
            )
        ]
    if hasher.digest() != decoded[1]:
        return [
            DataDefect(
                DAT_CHECKSUM,
                Severity.ERROR,
                f"asset '{key}' file:checksum does not match the bytes "
                f"(declared {hasher.name} digest differs from recomputed)",
                key,
                "file:checksum",
            )
        ]
    return []


def _verify_size(key: str, declared: Any, count: int) -> list[DataDefect]:
    if isinstance(declared, bool) or not isinstance(declared, int):
        return []  # absent or non-integer: PTL-AST-003 owns that
    if declared != count:
        return [
            DataDefect(
                DAT_SIZE,
                Severity.ERROR,
                f"asset '{key}' file:size is {declared} but the bytes are {count}",
                key,
                "file:size",
            )
        ]
    return []


def _verify_format(key: str, expected: str | None, head: bytes) -> list[DataDefect]:
    if expected is None:
        return []
    actual = _detect_format(head)
    if actual is None or actual == expected:
        return []
    return [
        DataDefect(
            DAT_FORMAT,
            Severity.ERROR,
            f"asset '{key}' declares {expected} but its bytes are {actual}",
            key,
            "type",
        )
    ]


def _check_raster(key: str, located: Locator) -> list[DataDefect]:
    """A raster asset MUST be a valid COG (formats.md:91) with embedded band stats."""
    defects: list[DataDefect] = []
    try:
        is_valid, errors, _warnings = cog_validate(located.gdal_path(), quiet=True)
    except Exception as exc:  # noqa: BLE001 - a reader failure is not a conformance fault
        return [
            DataDefect(
                DAT_COG,
                Severity.INFO,
                f"asset '{key}' could not be read as a raster ({exc})",
                key,
            )
        ]
    if not is_valid or errors:
        reason = errors[0] if errors else "not a cloud-optimized GeoTIFF"
        defects.append(
            DataDefect(
                DAT_COG,
                Severity.ERROR,
                f"asset '{key}' raster is not a valid cloud-optimized COG: {reason}",
                key,
                "type",
            )
        )
    defects.extend(_check_cog_stats(key, located))
    defects.extend(_check_overviews(key, located))
    defects.extend(_check_tile_size(key, located))
    return defects


# OGC 21-026 /req/optimized_geotiff/small-sizes: square internal tiles "sized
# no larger than a common screen viewport. 512×512 is the usual choice"
# (formats.md:121). Also the overview cutoff for untiled rasters, where no
# internal tile size exists to compare against.
_MAX_TILE = 512


def _tile_shape(src: Any) -> tuple[int, int] | None:
    """The internal tile's (height, width), or None for a striped file.

    Reads the profile's ``tiled`` flag directly (rasterio's ``is_tiled``
    convenience is pending deprecation); a striped file's blocks are rows,
    not tiles, so it has no tile shape.
    """
    if not src.profile.get("tiled", False):
        return None
    height, width = src.block_shapes[0]
    return height, width


def _check_overviews(key: str, located: Locator) -> list[DataDefect]:
    """A raster larger than one internal tile MUST carry internal overviews.

    A SHALL of OGC 21-026's Optimized GeoTIFF conformance class (optional in
    base COG, which is why ``cog_validate`` reports the absence as a warning
    only). formats.md:133: "A raster larger than a single internal tile needs
    internal overviews … one that already fits within a tile is exempt, since
    it is its own overview." The cutoff is the file's own tile size — a
    400px raster with 256px tiles needs overviews — falling back to 512 for
    untiled files (already a COG error, but reported by ``cog_validate``).
    External ``.ovr`` sidecars are already an error inside ``cog_validate``.
    """
    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(located.gdal_path()) as src:
            shape = _tile_shape(src)
            tile = max(shape) if shape is not None else _MAX_TILE
            oversized = max(src.width, src.height) > tile
            has_overviews = bool(src.overviews(1))
    except Exception:  # noqa: BLE001 - unreadable raster: the COG check owns reporting it
        return []
    if oversized and not has_overviews:
        return [
            DataDefect(
                DAT_OVERVIEWS,
                Severity.ERROR,
                f"asset '{key}' raster exceeds its own {tile}px internal tile "
                "but carries no internal overviews",
                key,
            )
        ]
    return []


def _check_tile_size(key: str, located: Locator) -> list[DataDefect]:
    """Internal tiles MUST be square and no larger than 512×512.

    OGC 21-026 /req/optimized_geotiff/small-sizes ("square internal tiles,
    sized no larger than a common screen viewport. 512×512 is the usual
    choice", formats.md:121), checked directly rather than delegated to
    ``rio-cogeo``, which enforces no explicit bound. Untiled rasters are
    skipped: tiling itself is a base-COG requirement that ``cog_validate``
    already reports through PTL-DAT-004.
    """
    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(located.gdal_path()) as src:
            shape = _tile_shape(src)
            if shape is None:
                return []
            tile_height, tile_width = shape
    except Exception:  # noqa: BLE001 - unreadable raster: the COG check owns reporting it
        return []
    if tile_height != tile_width:
        return [
            DataDefect(
                DAT_TILE_SIZE,
                Severity.ERROR,
                f"asset '{key}' internal tiles are {tile_width}x{tile_height}; "
                "tiles must be square",
                key,
            )
        ]
    if tile_width > _MAX_TILE:
        return [
            DataDefect(
                DAT_TILE_SIZE,
                Severity.ERROR,
                f"asset '{key}' internal tiles are {tile_width}x{tile_height}, "
                f"over the {_MAX_TILE}x{_MAX_TILE} limit",
                key,
            )
        ]
    return []


def _check_cog_stats(key: str, located: Locator) -> list[DataDefect]:
    """Every COG band MUST carry embedded min/max/mean/stddev (formats.md:95),
    and SHOULD carry a valid percent — a MUST when the band has a nodata value
    (formats.md:121)."""
    try:
        # GDAL_PAM_ENABLED=NO ignores any .aux.xml sidecar, so only statistics
        # embedded in the file's GDAL_METADATA tag count — as the spec requires.
        with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(located.gdal_path()) as src:
            missing: list[int] = []
            vp_should: list[int] = []  # valid percent absent, no nodata: SHOULD
            vp_must: list[int] = []  # valid percent absent with nodata: MUST
            for bidx in range(1, src.count + 1):
                tags = src.tags(bidx)
                if not all(stat in tags for stat in _COG_STAT_KEYS):
                    missing.append(bidx)
                if _VALID_PERCENT_KEY not in tags:
                    has_nodata = src.nodatavals[bidx - 1] is not None
                    (vp_must if has_nodata else vp_should).append(bidx)
    except Exception as exc:  # noqa: BLE001 - unreadable raster is advisory here
        return [
            DataDefect(
                DAT_COG_STATS,
                Severity.INFO,
                f"asset '{key}' band statistics could not be read ({exc})",
                key,
            )
        ]
    defects: list[DataDefect] = []
    if missing:
        defects.append(
            DataDefect(
                DAT_COG_STATS,
                Severity.ERROR,
                f"asset '{key}' band(s) {missing} lack embedded min/max/mean/stddev statistics",
                key,
            )
        )
    if vp_must:
        defects.append(
            DataDefect(
                DAT_VALID_PERCENT,
                Severity.ERROR,
                f"asset '{key}' band(s) {vp_must} have a nodata value but lack the "
                "embedded valid-percent statistic",
                key,
            )
        )
    if vp_should:
        defects.append(
            DataDefect(
                DAT_VALID_PERCENT,
                Severity.WARNING,
                f"asset '{key}' band(s) {vp_should} lack the embedded valid-percent statistic",
                key,
            )
        )
    return defects


def _check_consistency(
    node: Node, key: str, asset: dict[str, Any], expected: str, located: Locator
) -> list[DataDefect]:
    try:
        geo = _extract_geo(expected, located)
    except Exception as exc:  # noqa: BLE001 - unreadable header is advisory, not fatal
        return [
            DataDefect(
                DAT_CONSISTENCY,
                Severity.INFO,
                f"asset '{key}' spatial metadata could not be read ({exc})",
                key,
            )
        ]
    if geo is None:
        return []

    defects: list[DataDefect] = []
    declared_epsg = _declared_epsg(node, asset)
    if declared_epsg is not None and geo.epsg is not None and declared_epsg != geo.epsg:
        defects.append(
            DataDefect(
                DAT_CONSISTENCY,
                Severity.WARNING,
                f"asset '{key}' declares proj:epsg {declared_epsg} but its data is EPSG:{geo.epsg}",
                key,
            )
        )

    declared_bbox = _declared_bbox(node)
    bounds = _wgs84_bounds(geo)
    if declared_bbox is not None and bounds is not None and not _bbox_within(declared_bbox, bounds):
        message = _bbox_mismatch_message(key, declared_bbox, bounds, geo.epsg)
        defects.append(DataDefect(DAT_CONSISTENCY, Severity.WARNING, message, key))
    return defects


def _bbox_mismatch_message(
    key: str, declared: list[float], bounds: _Wgs84Bounds, epsg: int | None
) -> str:
    """Say what the declared bbox had to satisfy, not just that it failed."""
    if bounds.inner is None:
        return (
            f"asset '{key}' declared bbox {_fmt_bbox(declared)} is not contained by the "
            f"asset's extent {_fmt_bbox(bounds.outer)}"
        )
    if not bounds.reprojected:
        return (
            f"asset '{key}' data bbox {_fmt_bbox(bounds.outer)} does not match the "
            f"declared bbox {_fmt_bbox(declared)}"
        )
    return (
        f"asset '{key}' declared bbox {_fmt_bbox(declared)} is inconsistent with its "
        f"EPSG:{epsg} data, whose envelope contains {_fmt_bbox(bounds.inner)} and "
        f"lies within {_fmt_bbox(bounds.outer)}"
    )


# --- GeoParquet cloud-native structure -------------------------------------


def _check_geoparquet(key: str, located: Locator) -> list[DataDefect]:
    """A GeoParquet asset MUST have bounded row groups, per-row-group spatial
    statistics, and spatial ordering (formats.md:30,39,50).

    Parquet and GeoParquet share the ``application/vnd.apache.parquet`` media
    type. A file with no ``geo`` metadata key is plain Parquet — legitimate
    tabular data — so it is skipped rather than faulted; these rules apply only
    to actual GeoParquet.
    """
    try:
        source: Any = located.open_binary() if located.is_remote else located.source
        parquet = pq.ParquetFile(source)
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return []

    geo = _geo_metadata(parquet)
    if geo is None:
        return []  # plain Parquet, not GeoParquet — nothing to enforce here

    defects: list[DataDefect] = []
    defects.extend(_check_geoparquet_version(key, geo))
    meta = parquet.metadata
    row_counts = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
    if any(n > _MAX_ROW_GROUP_ROWS for n in row_counts):
        defects.append(
            DataDefect(
                DAT_ROWGROUP_SIZE,
                Severity.ERROR,
                f"asset '{key}' has a row group of {max(row_counts)} rows, "
                f"over the {_MAX_ROW_GROUP_ROWS} limit",
                key,
            )
        )

    bboxes, stat_defects = _rowgroup_stat_defects(key, parquet, geo)
    defects.extend(stat_defects)
    if bboxes is None:
        return defects  # without per-row-group boxes, ordering cannot be judged

    if not _is_spatially_ordered(bboxes):
        defects.append(
            DataDefect(
                DAT_ORDERING,
                Severity.ERROR,
                f"asset '{key}' rows are not spatially ordered: row groups overlap heavily "
                "and lack locality, so a reader cannot skip them",
                key,
            )
        )
    return defects


def _check_geoparquet_version(key: str, geo: dict[str, Any]) -> list[DataDefect]:
    """The ``geo`` metadata MUST declare GeoParquet 1.1 or 2.x.

    formats.md:25: "Data MUST be provided in GeoParquet 1.1 or 2.0". A file
    with ``geo`` metadata but an older version (1.0, the common legacy case)
    lacks the covering-column machinery the rest of the format MUSTs assume.
    """
    version = geo.get("version")
    if isinstance(version, str) and (
        version == "1.1" or version.startswith("1.1.") or version.startswith("2.")
    ):
        return []
    described = repr(version) if version is not None else "no version"
    return [
        DataDefect(
            DAT_GEOPARQUET_VERSION,
            Severity.ERROR,
            f"asset '{key}' geo metadata declares {described}; data must be GeoParquet 1.1 or 2.x",
            key,
        )
    ]


def _geo_metadata(parquet: Any) -> dict[str, Any] | None:
    raw = (parquet.schema_arrow.metadata or {}).get(b"geo")
    if raw is None:
        return None
    try:
        geo = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return geo if isinstance(geo, dict) else None


# --- item rollups -----------------------------------------------------------


def _check_rollup(
    node: Node, key: str, located: Locator, graph: CatalogGraph | None
) -> list[DataDefect]:
    """A rollup's rows MUST match the collection's items (PORTO-FMT-042).

    The comparison is on item ids, the one field both representations carry and
    the only one a client uses to join them. A rollup that has fallen behind
    reports items that no longer exist, or omits items that do, and the client
    reading it cannot tell either way.

    Skipped when the graph is unavailable, when the collection has no items to
    compare against, or when the rollup carries no ``id`` column to read. The
    last of those is reported: a file registered as a stac-geoparquet rollup
    without item ids is not one.
    """
    if graph is None:
        return []
    items = [child for child in graph.children_of(node) if child.kind == "item"]
    if not items:
        return []
    declared = {item.id for item in items if isinstance(item.id, str) and item.id}
    if not declared:
        return []  # unidentified items; PTL-STR/PTL-GEN own that gap

    try:
        source: Any = located.open_binary() if located.is_remote else located.source
        parquet = pq.ParquetFile(source)
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return []

    if "id" not in parquet.schema_arrow.names:
        return [
            DataDefect(
                DAT_ROLLUP,
                Severity.ERROR,
                f"rollup asset '{key}' has no 'id' column, so its rows cannot be matched"
                " to the collection's items",
                key,
            )
        ]

    try:
        column = parquet.read(columns=["id"]).column("id").to_pylist()
    except Exception:  # noqa: BLE001 - as above
        return []
    present = {value for value in column if isinstance(value, str)}

    missing = sorted(declared - present)
    extra = sorted(present - declared)
    if not missing and not extra:
        return []

    parts = []
    if missing:
        parts.append(f"{len(missing)} item(s) absent from the rollup ({_sample(missing)})")
    if extra:
        parts.append(f"{len(extra)} rollup row(s) with no item ({_sample(extra)})")
    return [
        DataDefect(
            DAT_ROLLUP,
            Severity.ERROR,
            f"rollup asset '{key}' disagrees with the collection's items: " + ", ".join(parts),
            key,
        )
    ]


def _sample(ids: list[str], limit: int = 3) -> str:
    shown = ", ".join(ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, …"


# --- tabular collections ----------------------------------------------------


def _check_tabular(
    node: Node, key: str, asset: dict[str, Any], located: Locator
) -> list[DataDefect]:
    """The Tabular Data SHOULDs for a plain-Parquet collection-level data asset.

    formats.md, Tabular Data: "Tabular collections SHOULD populate
    ``extent.temporal`` when the data has a time dimension, and SHOULD document
    their columns with the STAC table extension (``table:columns`` with names,
    types, and descriptions)." A tabular collection is a Parquet data asset
    with no ``geo`` metadata key, exposed at collection level (the single-file
    collection pattern the same section mandates); non-data Parquet and
    item-level assets are out of scope. The time dimension is read from the
    file itself: a temporal (timestamp/date) column is the machine-detectable
    signal, so its absence keeps the extent SHOULD silent rather than guessed.
    """
    if node.kind != "collection" or "data" not in _asset_roles(asset):
        return []
    try:
        source: Any = located.open_binary() if located.is_remote else located.source
        parquet = pq.ParquetFile(source)
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return []
    if _geo_metadata(parquet) is not None:
        return []  # GeoParquet: the geospatial storage rules own it
    defects: list[DataDefect] = []
    if not _has_table_columns(node, asset):
        defects.append(
            DataDefect(
                DAT_TABULAR,
                Severity.WARNING,
                f"tabular asset '{key}': the collection does not document its columns "
                "with the table extension (table:columns)",
                key,
            )
        )
    if _has_temporal_column(parquet) and not _has_temporal_extent(node):
        defects.append(
            DataDefect(
                DAT_TABULAR,
                Severity.WARNING,
                f"tabular asset '{key}' carries a temporal column but the collection "
                "does not populate extent.temporal",
                key,
            )
        )
    return defects


def _asset_roles(asset: dict[str, Any]) -> list[str]:
    raw = asset.get("roles")
    if not isinstance(raw, list):
        return []
    return [role for role in raw if isinstance(role, str)]


def _has_table_columns(node: Node, asset: dict[str, Any]) -> bool:
    """The table extension allows ``table:columns`` on the collection or on
    the asset itself; the reference catalog uses the asset."""
    for columns in (node.data.get("table:columns"), asset.get("table:columns")):
        if isinstance(columns, list) and len(columns) > 0:
            return True
    return False


def _has_temporal_column(parquet: Any) -> bool:
    return any(pa.types.is_temporal(field.type) for field in parquet.schema_arrow)


def _has_temporal_extent(node: Node) -> bool:
    extent = node.data.get("extent")
    temporal = extent.get("temporal") if isinstance(extent, dict) else None
    intervals = temporal.get("interval") if isinstance(temporal, dict) else None
    if not isinstance(intervals, list):
        return False
    return any(
        isinstance(interval, list) and any(bound is not None for bound in interval)
        for interval in intervals
    )


def _rowgroup_stat_defects(
    key: str, parquet: Any, geo: dict[str, Any]
) -> tuple[list[tuple[float, float, float, float]] | None, list[DataDefect]]:
    """Per-row-group [minx, miny, maxx, maxy] boxes, plus statistics defects.

    formats.md:39 accepts two satisfiers: a 1.1 ``bbox`` covering column whose
    leaf fields carry Parquet min/max, or — for GeoParquet 2.x / Parquet
    ``GEOMETRY`` — native ``GeospatialStatistics`` per row group. Neither
    source yielding a box for every row group is the MUST failure (ERROR).
    Native statistics alone still satisfy the MUST, but formats.md keeps the
    covering column "RECOMMENDED even where native statistics exist, since it
    adds page-level min/max stats that enable finer-grained pruning" — that
    SHOULD surfaces as a WARNING.
    """
    boxes = _covering_bboxes(parquet, geo)
    if boxes is not None:
        return boxes, []
    boxes = _native_bboxes(parquet, geo)
    if boxes is None:
        return None, [
            DataDefect(
                DAT_ROWGROUP_STATS,
                Severity.ERROR,
                f"asset '{key}' provides no per-row-group spatial statistics "
                "(no bbox covering column with min/max stats, nor native GeospatialStatistics)",
                key,
            )
        ]
    return boxes, [
        DataDefect(
            DAT_ROWGROUP_STATS,
            Severity.WARNING,
            f"asset '{key}' relies on native GeospatialStatistics without a bbox "
            "covering column; a covering column remains recommended for "
            "page-level pruning",
            key,
        )
    ]


def _native_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float]] | None:
    """Per-row-group boxes from Parquet native ``GeospatialStatistics``.

    Read from the primary geometry column's chunk metadata (pyarrow >= 21
    exposes ``geo_statistics``; older versions have no attribute and fall
    through to None, keeping the covering column as the only satisfier).
    """
    primary = geo.get("primary_column")
    if not isinstance(primary, str):
        return None
    meta = parquet.metadata
    index = _column_index(meta)
    j = index.get(primary)
    if j is None:
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for i in range(meta.num_row_groups):
        stats = getattr(meta.row_group(i).column(j), "geo_statistics", None)
        if stats is None:
            return None
        corners = (stats.xmin, stats.ymin, stats.xmax, stats.ymax)
        if None in corners:
            return None
        boxes.append(tuple(float(c) for c in corners))  # type: ignore[arg-type]
    return boxes


def _covering_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float]] | None:
    """Per-row-group [minx, miny, maxx, maxy] from the bbox covering column's stats.

    Returns None when the file has no 1.1 ``bbox`` covering column whose leaf
    fields carry Parquet min/max statistics.
    """
    primary = geo.get("primary_column")
    columns = geo.get("columns")
    if not isinstance(columns, dict):
        return None
    covering = columns.get(primary, {}).get("covering", {}).get("bbox")
    if not isinstance(covering, dict):
        return None
    try:
        paths = {corner: ".".join(covering[corner]) for corner in ("xmin", "ymin", "xmax", "ymax")}
    except (KeyError, TypeError):
        return None

    meta = parquet.metadata
    index = _column_index(meta)
    if not all(path in index for path in paths.values()):
        return None

    boxes: list[tuple[float, float, float, float]] = []
    for i in range(meta.num_row_groups):
        group = meta.row_group(i)
        try:
            minx = group.column(index[paths["xmin"]]).statistics.min
            miny = group.column(index[paths["ymin"]]).statistics.min
            maxx = group.column(index[paths["xmax"]]).statistics.max
            maxy = group.column(index[paths["ymax"]]).statistics.max
        except AttributeError:
            return None  # a leaf without statistics does not qualify
        if None in (minx, miny, maxx, maxy):
            return None
        boxes.append((float(minx), float(miny), float(maxx), float(maxy)))
    return boxes


def _column_index(meta: Any) -> dict[str, int]:
    if meta.num_row_groups == 0:
        return {}
    group = meta.row_group(0)
    return {group.column(j).path_in_schema: j for j in range(group.num_columns)}


def _is_spatially_ordered(bboxes: list[tuple[float, float, float, float]]) -> bool:
    """True if row groups are spatially ordered by either spec criterion (formats.md:30)."""
    if len(bboxes) <= 1:
        return True
    pairs = len(bboxes) - 1
    overlaps = sum(_bbox_overlaps(bboxes[i], bboxes[i + 1]) for i in range(pairs))
    if overlaps / pairs < _MAX_OVERLAP_FRACTION:
        return True  # low overlap

    extent = _bbox_union(bboxes)
    extent_area = _bbox_area(extent)
    if extent_area == 0:
        return True  # a single location — nothing to order
    mean_ratio = sum(_bbox_area(b) for b in bboxes) / len(bboxes) / extent_area
    return mean_ratio < _MAX_LOCALITY_RATIO  # high locality


def _bbox_area(b: tuple[float, float, float, float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _bbox_overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _bbox_union(
    bboxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


# --- partition schema consistency ------------------------------------------


def _check_partition_schemas(node: Node) -> list[DataDefect]:
    """Every partition file MUST share a single Parquet schema.

    formats.md:91: "Every partition file MUST share a single Parquet schema —
    the same columns, names, and types — so the glob can be queried as one
    table. This is validated by tooling reading file footers, not by JSON
    schema." A local relative ``partition:glob`` is expanded against the
    collection's own directory and every matched footer is compared to the
    first; a remote or absolute glob (``s3://``, ``https://``) cannot be
    listed from the local tree, so it is skipped. Unreadable files degrade to
    silence — the byte checks own reporting broken assets.
    """
    pattern = node.data.get("partition:glob")
    if not isinstance(pattern, str) or not pattern.strip():
        return []
    if urlparse(pattern).scheme or pattern.startswith("/"):
        return []  # remote or absolute: not listable from here
    normalized = posixpath.normpath(pattern)
    if normalized.startswith(".."):
        return []  # escapes the catalog tree
    base = node.abs_path.parent
    schemas: list[tuple[str, dict[str, str]]] = []
    for match in sorted(globmodule.glob(normalized, root_dir=str(base))):
        try:
            parquet = pq.ParquetFile(str(base / match))
        except Exception:  # noqa: BLE001 # nosec B112 - unreadable partition: the byte checks own it
            continue
        arrow = parquet.schema_arrow
        schemas.append((match, {field.name: str(field.type) for field in arrow}))
    if len(schemas) < 2:
        return []  # nothing to compare
    reference_name, reference = schemas[0]
    defects: list[DataDefect] = []
    for name, schema in schemas[1:]:
        difference = _schema_difference(reference, schema)
        if difference is None:
            continue
        defects.append(
            DataDefect(
                DAT_PARTITION_SCHEMA,
                Severity.ERROR,
                f"partition file '{name}' does not share '{reference_name}'s "
                f"Parquet schema: {difference}",
                "",
                json_pointer="/partition:glob",
            )
        )
    return defects


def _schema_difference(reference: dict[str, str], other: dict[str, str]) -> str | None:
    """A one-line description of how two column maps diverge, or None if equal."""
    missing = sorted(set(reference) - set(other))
    extra = sorted(set(other) - set(reference))
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing column(s) {missing}")
        if extra:
            parts.append(f"extra column(s) {extra}")
        return ", ".join(parts)
    for name in reference:
        if reference[name] != other[name]:
            return f"column '{name}' is {other[name]}, expected {reference[name]}"
    return None


# --- format probing --------------------------------------------------------


def _expected_format(media_type: str) -> str | None:
    lowered = media_type.lower()
    if "parquet" in lowered:
        return "parquet"
    if lowered.startswith("image/tiff"):
        return "tiff"
    if "pmtiles" in lowered:
        return "pmtiles"
    if lowered.startswith("image/png"):
        return "png"
    if lowered.startswith("image/jpeg") or lowered.startswith("image/jpg"):
        return "jpeg"
    return None


def _detect_format(head: bytes) -> str | None:
    if head[:4] == b"PAR1":
        return "parquet"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[: len(_PMTILES_MAGIC)] == _PMTILES_MAGIC:
        return "pmtiles"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None


# --- spatial extraction ----------------------------------------------------


def _extract_geo(expected: str, located: Locator) -> _Geo | None:
    if expected == "parquet":
        return _geo_from_parquet(located)
    if expected == "tiff":
        return _geo_from_raster(located)
    if expected == "pmtiles":
        return _geo_from_pmtiles(located)
    return None


def _geo_from_parquet(located: Locator) -> _Geo | None:
    source: Any = located.open_binary() if located.is_remote else located.source
    parquet = pq.ParquetFile(source)
    raw = (parquet.schema_arrow.metadata or {}).get(b"geo")
    if raw is None:
        return None
    geo = json.loads(raw)
    primary = geo.get("primary_column")
    column = geo.get("columns", {}).get(primary, {})
    bbox = column.get("bbox")
    crs_obj = column.get("crs")
    crs = CRS.from_user_input(crs_obj) if crs_obj is not None else CRS.from_epsg(4326)
    return _Geo(bbox=_as_bbox(bbox), epsg=crs.to_epsg(), crs=crs)


def _geo_from_raster(located: Locator) -> _Geo | None:
    with rasterio.open(located.gdal_path()) as src:
        bounds = src.bounds
        crs = CRS.from_wkt(src.crs.to_wkt()) if src.crs else None
        return _Geo(
            bbox=[bounds.left, bounds.bottom, bounds.right, bounds.top],
            epsg=crs.to_epsg() if crs else None,
            crs=crs,
            # the grid extent, which a nodata collar can hold well inside it
            tight=False,
        )


def _geo_from_pmtiles(located: Locator) -> _Geo | None:
    with located.open_binary() as handle:
        header = handle.read(127)
    if len(header) < 127 or header[:7] != _PMTILES_MAGIC:
        return None
    # v3 header: min/max lon/lat are int32 E7 at byte offsets 102, 106, 110, 114.
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<iiii", header, 102)
    bbox = [min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7]
    return _Geo(bbox=bbox, epsg=4326, crs=CRS.from_epsg(4326))


def _as_bbox(bbox: Any) -> list[float] | None:
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None
    try:
        values = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return None
    # Drop any z coordinates: [minx, miny, (minz,) maxx, maxy, (maxz)].
    if len(values) == 6:
        return [values[0], values[1], values[3], values[4]]
    return values[:4]


def _wgs84_bounds(geo: _Geo) -> _Wgs84Bounds | None:
    """Bracket the WGS84 envelope of the data behind a native bbox."""
    if geo.bbox is None:
        return None
    if geo.crs is None or geo.epsg == 4326:
        return _Wgs84Bounds(outer=geo.bbox, inner=geo.bbox if geo.tight else None)
    transformer = Transformer.from_crs(geo.crs, CRS.from_epsg(4326), always_xy=True)
    minx, miny, maxx, maxy = geo.bbox[:4]
    outer = list(transformer.transform_bounds(minx, miny, maxx, maxy, densify_pts=_DENSIFY_PTS))
    if not all(math.isfinite(v) for v in outer):
        return None
    if outer[0] > outer[2]:
        # transform_bounds signals an antimeridian crossing by returning a box
        # that wraps; no plain min/max comparison means anything across the seam
        return None
    inner = _inner_bounds(transformer, geo.bbox) if geo.tight else None
    if geo.tight and inner is None:
        return None
    return _Wgs84Bounds(outer=outer, inner=inner, reprojected=True)


def _inner_bounds(transformer: Transformer, bbox: list[float]) -> list[float] | None:
    """The largest box the reprojected envelope is guaranteed to *contain*.

    A native bbox is tight, so some geometry touches each of its four edges.
    Reprojecting one edge therefore pins one side of the true envelope from the
    inside: the touching point's longitude is at most that edge's largest
    longitude, so the envelope's own minimum longitude is at most that too, and
    symmetrically for the other three sides. Taking the tightest such bound over
    all four edges holds however the projection reorients the rectangle.
    """
    minx, miny, maxx, maxy = bbox[:4]
    steps = [i / (_DENSIFY_PTS + 1) for i in range(_DENSIFY_PTS + 2)]
    min_lon_at_most = min_lat_at_most = math.inf
    max_lon_at_least = max_lat_at_least = -math.inf
    for x0, y0, x1, y1 in (
        (minx, miny, minx, maxy),
        (maxx, miny, maxx, maxy),
        (minx, miny, maxx, miny),
        (minx, maxy, maxx, maxy),
    ):
        lons, lats = transformer.transform(
            [x0 + (x1 - x0) * f for f in steps], [y0 + (y1 - y0) * f for f in steps]
        )
        if not all(math.isfinite(v) for v in (*lons, *lats)):
            return None
        min_lon_at_most = min(min_lon_at_most, max(lons))
        min_lat_at_most = min(min_lat_at_most, max(lats))
        max_lon_at_least = max(max_lon_at_least, min(lons))
        max_lat_at_least = max(max_lat_at_least, min(lats))
    return [min_lon_at_most, min_lat_at_most, max_lon_at_least, max_lat_at_least]


def _bbox_within(declared: list[float], bounds: _Wgs84Bounds) -> bool:
    """Does a declared bbox sit inside the bracket, side by side?

    Every side must land inside ``outer``. When ``inner`` is known each side must
    also reach at least that far, which is what keeps an under-declared bbox
    detectable; where the two boxes coincide the pair collapses to plain equality
    within the tolerance.
    """
    minx, miny, maxx, maxy = declared[:4]
    west, south, east, north = bounds.outer
    if not all(west - _BBOX_TOL <= v <= east + _BBOX_TOL for v in (minx, maxx)):
        return False
    if not all(south - _BBOX_TOL <= v <= north + _BBOX_TOL for v in (miny, maxy)):
        return False
    if bounds.inner is None:
        return True
    reach_w, reach_s, reach_e, reach_n = bounds.inner
    return (
        minx <= reach_w + _BBOX_TOL
        and miny <= reach_s + _BBOX_TOL
        and maxx >= reach_e - _BBOX_TOL
        and maxy >= reach_n - _BBOX_TOL
    )


def _fmt_bbox(bbox: list[float]) -> str:
    return "[" + ", ".join(f"{v:.4f}" for v in bbox[:4]) + "]"


def _declared_bbox(node: Node) -> list[float] | None:
    if node.kind == "item":
        return _as_bbox(node.data.get("bbox"))
    extent = node.data.get("extent", {})
    spatial = extent.get("spatial", {}) if isinstance(extent, dict) else {}
    boxes = spatial.get("bbox") if isinstance(spatial, dict) else None
    if isinstance(boxes, list) and boxes:
        return _as_bbox(boxes[0])
    return None


def _declared_epsg(node: Node, asset: dict[str, Any]) -> int | None:
    for source in (asset, node.data.get("properties", {}), node.data):
        if isinstance(source, dict):
            value = source.get("proj:epsg")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None
