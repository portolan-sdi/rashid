"""Byte-level checks: does an asset's data match what its metadata declares?

Reached only through :func:`rashid.data.default_validator`, so importing this
module (and the geospatial stack it pulls) happens only when the opt-in data
pass actually runs. Each check turns a divergence between the declared metadata
and the real bytes into a :class:`rashid.data.DataDefect`:

- ``PTL-DAT-001`` recomputed multihash ≠ ``file:checksum`` (MUST)
- ``PTL-DAT-002`` byte length ≠ ``file:size`` (MUST)
- ``PTL-DAT-003`` magic bytes ≠ declared media type (MUST)
- ``PTL-DAT-004`` a raster asset is not a valid COG (MUST, formats.md:91)
- ``PTL-DAT-005`` actual bbox/CRS inconsistent with the declared metadata
  (advisory). A ``proj:epsg`` the asset declares itself is faulted on the asset;
  one inherited from the enclosing collection or item is faulted at the field
  that declares it, once, however many assets read it.
- ``PTL-DAT-006`` GeoParquet rows are not spatially ordered (MUST, formats.md:30)
- ``PTL-DAT-007`` no per-row-group spatial statistics (MUST, formats.md:64)
- ``PTL-DAT-008`` a row group exceeds 150,000 rows (MUST, formats.md:75)
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
- ``PTL-DAT-016`` an item mirror diverges from the collection's items in row
  count, ids, geometry, datetime, or bbox (MUST, formats.md, Raster § Item
  mirror). A mirror runs the GeoParquet checks above as well: the spec binds
  it to them like any other spatial table.

The four GeoParquet checks — ``PTL-DAT-006``, ``007``, ``008``, and ``012`` —
run over an asset and over every file a local relative ``partition:glob``
matches. A partitioned collection publishes its data through the glob and
declares no ``data`` asset, so the glob is the only place those bytes appear.
Each rule reports once per collection, at ``/partition:glob``, naming how many
partition files failed.

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
import random
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
import shapely
from pyproj import CRS, Transformer
from rio_cogeo.cogeo import cog_validate
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from rashid._multihash import decode_multihash
from rashid.catalog import CatalogGraph, Node
from rashid.data import (
    DAT_CHECKSUM,
    DAT_COG,
    DAT_COG_STATS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_GEOPARQUET_VERSION,
    DAT_MIRROR,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SIZE,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
    DAT_VECTOR_COLUMNS,
    DataDefect,
)
from rashid.data.reader import AssetReader, Locator
from rashid.model import Severity
from rashid.rules.item_mirror import is_mirror_asset

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

# formats.md:75 — a GeoParquet row group MUST hold no more than this many rows.
_MAX_ROW_GROUP_ROWS = 150_000

# formats.md:38 — the footer check estimates how many row groups a query window
# covering _QUERY_FRACTION of each dimension lets a reader skip, and compares that
# against an ideal grid tiling of the extent into the same number of row groups. A
# file passes at half the achievable rate.
_QUERY_FRACTION = 0.10
_QUERY_SAMPLES = 20
_QUERY_SEED = 42
# 0.70 is placed from real data: across 206 files with five or more row groups from
# every catalog in the Portolan registry, one genuinely unsorted file scored 0.00,
# eight under-sorted files fell between 0.53 and 0.70, and the remaining 197 ran
# from 0.72 up with a median of 0.98. Every file in that band reached 0.87-0.97
# after a spatial sort, so the bar separates files a re-sort would measurably
# improve from files already as good as their row-group count allows.
_MIN_SKIP_EFFICIENCY = 0.70

# The row rule keeps a flat limit on how much of the extent a chunk's box covers.
# Its reference never moves — the rows are always split into _ORDERING_CHUNKS
# groups, so perfect tiling is always about 1/10 of the extent — which is why a
# flat figure is principled here and a relative one is needed for the footer
# check, whose row-group count varies. Measured over ten chunks: Hilbert-sorted
# clustered data averages 0.20 of the extent, x-only sorted 0.10 and a sorted
# coastline 0.13, while rows scattered within two far-apart bands average 0.45
# and globally unsorted rows 1.00. The limit sits in that gap.
_MAX_LOCALITY_RATIO = 0.30

# formats.md:56 — below five row groups the grid reference is unreliable rather than
# the threshold unreachable. Measured on Hilbert-sorted clustered data, a perfectly
# sorted file reaches as little as 11% of the grid's rate at two row groups and 30%
# at three, because a grid of two or three cells is a poor model of what a sort can
# achieve on clustered data. PORTO-FMT-044 forbids failing a file on a threshold its
# row-group count puts out of reach, so below five groups the check does not run.
_MIN_ORDERING_ROW_GROUPS = 5

# Row ordering is a separate rule that applies to every file, whatever its row-group
# layout (formats.md:30). It is checked by splitting the rows into the groups a
# conforming writer would have produced, which works at any row-group count and is
# the only check available below five. Ten chunks is comfortably above
# _MIN_ORDERING_ROW_GROUPS, so the 30% limit means something; below the row floor a
# chunk covers too few points to say anything. Neither number is a spec threshold.
_ORDERING_CHUNKS = 10
_MIN_CHUNK_ROWS = 20

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
class _EpsgDeclaration:
    """A ``proj:epsg`` value and the document node that carries it.

    ``pointer`` is ``None`` when the asset declares the value itself. Otherwise
    the asset inherits it from the enclosing collection or item, and ``pointer``
    locates the single field that governs every asset on that object.
    """

    value: int
    pointer: str | None


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

    ``graph`` is optional because one check, the item mirror's agreement with
    the collection's items, needs the object's children. Without it that check
    is skipped.
    """
    defects: list[DataDefect] = []
    defects.extend(_check_partition_schemas(node))
    defects.extend(_check_partition_geoparquet(node, reader))
    for key, asset in _assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            continue  # PTL-AST-001 reports a missing href
        defects.extend(_check_asset(node, key, asset, href, reader, graph))
    return _collapse_shared_fields(defects)


def _collapse_shared_fields(defects: list[DataDefect]) -> list[DataDefect]:
    """Report a defect in one shared field once, not once per asset that reads it.

    Every asset on a collection inherits the collection's ``proj:epsg``, so the
    per-asset pass raises the same defect against the same field as many times
    as the collection has assets. One declaration is one defect. Defects that
    name an asset carry no pointer of their own and pass through untouched.
    """
    seen: set[tuple[str, str, str]] = set()
    kept: list[DataDefect] = []
    for defect in defects:
        if defect.json_pointer is None:
            kept.append(defect)
            continue
        signature = (defect.rule_id, defect.json_pointer, defect.message)
        if signature in seen:
            continue
        seen.add(signature)
        kept.append(defect)
    return kept


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
    if located is None or _is_source(asset):
        # PORTO-FMT-045: the format requirements apply to the catalog's own
        # cloud-native assets, and a source asset is not one. It names the
        # upstream original a cloud-native asset was derived from, which is a
        # Shapefile or a GeoPackage as often as not, so judging it as a COG or
        # a GeoParquet would fail it for being what it says it is. Its bytes
        # are still checksum/size/format-verified above.
        return defects
    if is_mirror_asset(asset):
        # formats.md, Raster § Item mirror: a mirror must reproduce the items
        # it mirrors (PORTO-FMT-042). It then falls through to the GeoParquet
        # checks below, which bind it as they bind vector data
        # (PORTO-FMT-043) — an item index is queried by extent like any other
        # spatial table. The tabular SHOULDs skip it on their own, being
        # scoped to assets with the 'data' role.
        defects.extend(_check_mirror(node, key, located, graph))
    if expected == "tiff":
        defects.extend(_check_raster(key, located))
    if expected == "parquet":
        defects.extend(_check_geoparquet(key, located))
        defects.extend(_check_tabular(node, key, asset, located))
        defects.extend(_check_vector_columns(node, key, asset, located))
    if expected in {"parquet", "tiff", "pmtiles"}:
        defects.extend(_check_consistency(node, key, asset, expected, located))
    return defects


def _is_source(asset: dict[str, Any]) -> bool:
    """Whether the asset carries the ``source`` role.

    Only ``source``. An earlier version also matched ``alternate``, which is
    not a role: the Alternate Assets extension defines an ``alternate`` field
    holding other locations for the same bytes, and core.md uses "alternate"
    as prose for a non-primary representation. Neither makes it a role name,
    and no catalog emitted one.
    """
    roles = asset.get("roles")
    if not isinstance(roles, list):
        return False
    return any(isinstance(role, str) and role == "source" for role in roles)


def _check_bytes(
    key: str,
    asset: dict[str, Any],
    href: str,
    expected: str | None,
    node: Node,
    reader: AssetReader,
) -> list[DataDefect]:
    """Verify checksum, size, and format magic, reading only what they need.

    The three findings do not cost the same. A recomputed digest and a byte
    counter each need every byte; the format magic needs ``_HEAD_BYTES``. So the
    metadata decides how much of the object is read, and an asset that declares
    none of the three is never fetched — for a catalog of remote COGs that is
    the difference between a full download per asset and no request at all
    (#86). What is reported does not change: the same three verifications run on
    the same bytes they always looked at.

    Two absences are what they look like elsewhere in this module. A
    ``file:checksum`` naming a hash function rashid cannot compute yields no
    hasher, and :func:`_verify_checksum` answers it with an INFO drawn from the
    declaration alone, so those bytes would settle nothing. A ``file:size``
    that is not an integer is absent to :func:`_verify_size`, and the guard here
    is that same test so the two cannot disagree.

    The stream is still requested up front, and ``None`` still means silence:
    both readers build it lazily (``_http_stream`` is a generator, so no request
    leaves until it is iterated), which makes asking for one the cheapest
    fetchability test available through the ``AssetReader`` protocol.
    """
    stream = reader.stream(node, href)
    if stream is None:
        return []  # not fetchable; metadata pass owns missing/foreign hrefs

    decoded = decode_multihash(asset.get("file:checksum"))
    algo, hasher = _hasher_for(decoded)
    declared_size = asset.get("file:size")
    sized = isinstance(declared_size, int) and not isinstance(declared_size, bool)

    if hasher is None and not sized:
        if expected is None:
            _close(stream)
            return _verify_checksum(key, decoded, algo, hasher)
        try:
            head = _read_head(stream)
        except OSError as exc:
            return [_unread(key, exc)]
        return _verify_checksum(key, decoded, algo, hasher) + _verify_format(key, expected, head)

    try:
        head, count = _consume(stream, hasher)
    except OSError as exc:
        return [_unread(key, exc)]

    defects: list[DataDefect] = []
    defects.extend(_verify_checksum(key, decoded, algo, hasher))
    defects.extend(_verify_size(key, declared_size, count))
    defects.extend(_verify_format(key, expected, head))
    return defects


def _hasher_for(decoded: tuple[int, bytes] | None) -> tuple[str | None, Any]:
    """The hashlib name and hasher for a decoded multihash, or ``(None, None)``.

    A code outside ``_HASH_ALGOS`` leaves both None: the digest cannot be
    recomputed, and :func:`_verify_checksum` reports that from the code alone.
    """
    if decoded is None:
        return None, None
    algo = _HASH_ALGOS.get(decoded[0])
    return algo, hashlib.new(algo) if algo is not None else None


def _consume(stream: Iterator[bytes], hasher: Any) -> tuple[bytes, int]:
    """Read the whole object, returning its head and its byte count."""
    head = b""
    count = 0
    for chunk in stream:
        count += len(chunk)
        if len(head) < _HEAD_BYTES:
            head += chunk[: _HEAD_BYTES - len(head)]
        if hasher is not None:
            hasher.update(chunk)
    return head, count


def _read_head(stream: Iterator[bytes]) -> bytes:
    """The object's first ``_HEAD_BYTES``, then stop pulling.

    Every probe in :func:`_detect_format` reads within that prefix, so the rest
    of the object cannot change the answer. A short object simply ends first.
    """
    head = b""
    try:
        for chunk in stream:
            head += chunk[: _HEAD_BYTES - len(head)]
            if len(head) >= _HEAD_BYTES:
                break
    finally:
        _close(stream)
    return head


def _close(stream: Iterator[bytes]) -> None:
    """Release a stream this check is done with, rather than leave it to the collector.

    ``_http_stream`` is a generator holding an open ``urlopen`` response, so a
    stream dropped part-way keeps a socket until it is reclaimed.
    ``AssetReader.stream`` promises only an ``Iterator[bytes]``, which need not
    be closeable, hence the guard.
    """
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _unread(key: str, exc: OSError) -> DataDefect:
    """A read that failed is unverified, not nonconformant."""
    return DataDefect(
        DAT_CHECKSUM,
        Severity.INFO,
        f"asset '{key}' bytes could not be read ({exc}); not verified",
        key,
    )


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
                "which rashid cannot compute; not verified",
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
                expected=hasher.digest().hex(),
                actual=decoded[1].hex(),
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
                expected=count,
                actual=declared,
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
    if declared_epsg is not None and geo.epsg is not None and declared_epsg.value != geo.epsg:
        defects.append(_epsg_defect(node, key, declared_epsg, geo.epsg))

    declared_bbox = _declared_bbox(node)
    bounds = _wgs84_bounds(geo)
    if declared_bbox is not None and bounds is not None and not _bbox_within(declared_bbox, bounds):
        message = _bbox_mismatch_message(key, declared_bbox, bounds, geo.epsg)
        defects.append(DataDefect(DAT_CONSISTENCY, Severity.WARNING, message, key))
    return defects


def _epsg_defect(node: Node, key: str, declared: _EpsgDeclaration, actual: int) -> DataDefect:
    """Fault the document node that carries the declaration, not its inheritor.

    An asset that declares its own ``proj:epsg`` and contradicts its own bytes
    is faulted on the asset: the two claims are about the same object, and only
    the publisher knows which one is true. A value inherited from the enclosing
    collection or item is faulted where it is written, and the message says the
    assets inherit it, because the field an editor must change is that one and
    the asset the reader was sent to holds no ``proj:epsg`` at all. The file's
    own spatial metadata settles the inherited case, so ``expected`` carries the
    CRS the bytes declare and ``actual`` the stale value in the document.
    """
    if declared.pointer is None:
        return DataDefect(
            DAT_CONSISTENCY,
            Severity.WARNING,
            f"asset '{key}' declares proj:epsg {declared.value} but its data is EPSG:{actual}",
            key,
            expected=actual,
            actual=declared.value,
        )
    where = "in its properties" if declared.pointer.startswith("/properties") else "at its root"
    return DataDefect(
        DAT_CONSISTENCY,
        Severity.WARNING,
        f"{node.kind} '{node.id}' declares proj:epsg {declared.value} {where}, which its "
        f"assets inherit, but the asset data is EPSG:{actual}",
        key,
        json_pointer=declared.pointer,
        expected=actual,
        actual=declared.value,
    )


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


def _check_geoparquet(
    key: str, located: Locator, *, subject: str | None = None
) -> list[DataDefect]:
    """A GeoParquet asset MUST have bounded row groups, per-row-group spatial
    statistics, and spatial ordering (formats.md:30,64,75).

    Parquet and GeoParquet share the ``application/vnd.apache.parquet`` media
    type. A file with no ``geo`` metadata key is plain Parquet — legitimate
    tabular data — so it is skipped rather than faulted; these rules apply only
    to actual GeoParquet.

    ``subject`` names the file in the message. It defaults to the asset the key
    belongs to; the partition pass passes the matched path instead, since a
    partition file is addressed by the collection's glob and is nobody's asset.
    """
    subject = subject or f"asset '{key}'"
    try:
        source: Any = located.open_binary() if located.is_remote else located.source
        parquet = pq.ParquetFile(source)
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return []

    geo = _geo_metadata(parquet)
    if geo is None:
        return []  # plain Parquet, not GeoParquet — nothing to enforce here

    defects: list[DataDefect] = []
    defects.extend(_check_geoparquet_version(key, geo, subject=subject))
    meta = parquet.metadata
    row_counts = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
    if any(n > _MAX_ROW_GROUP_ROWS for n in row_counts):
        defects.append(
            DataDefect(
                DAT_ROWGROUP_SIZE,
                Severity.ERROR,
                f"{subject} has a row group of {max(row_counts)} rows, "
                f"over the {_MAX_ROW_GROUP_ROWS} limit",
                key,
            )
        )

    bboxes, stat_defects = _rowgroup_stat_defects(key, parquet, geo, subject=subject)
    defects.extend(stat_defects)
    if bboxes is None:
        return defects  # without per-row-group boxes, ordering cannot be judged

    groups = len(bboxes)
    metrics_apply = groups >= _MIN_ORDERING_ROW_GROUPS

    # A failed footer check settles the rule. A passing footer check can also
    # settle the row check when conservative footer bounds prove that the
    # synthetic row chunks pass. Otherwise inspect the rows themselves.
    if metrics_apply and not _is_spatially_ordered(bboxes):
        defects.append(
            DataDefect(
                DAT_ORDERING,
                Severity.ERROR,
                f"{subject} rows are not spatially ordered: row groups overlap heavily "
                "and lack locality, so a reader cannot skip them",
                key,
            )
        )
        return defects
    if metrics_apply and _footer_proves_row_ordering(parquet, geo, bboxes, row_counts):
        return defects

    row_defects = _row_ordering_defects(
        key,
        parquet,
        geo,
        sum(row_counts),
        groups,
        report_unreadable=not metrics_apply,
        subject=subject,
    )
    defects.extend(row_defects)
    return defects


def _check_geoparquet_version(
    key: str, geo: dict[str, Any], *, subject: str | None = None
) -> list[DataDefect]:
    """The ``geo`` metadata MUST declare GeoParquet 1.1 or 2.x.

    formats.md:25: "Data MUST be provided in GeoParquet 1.1 or 2.0". A file
    with ``geo`` metadata but an older version (1.0, the common legacy case)
    lacks the covering-column machinery the rest of the format MUSTs assume.
    """
    subject = subject or f"asset '{key}'"
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
            f"{subject} geo metadata declares {described}; data must be GeoParquet 1.1 or 2.x",
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


# --- item mirrors -----------------------------------------------------------


# Coordinate agreement tolerance, in degrees, for the mirror comparison. Item
# JSON and WKB both carry float64, so the only divergence a conformant pair
# shows is decimal formatting on the JSON side: a coordinate written to six
# decimals lands within 5e-7 of the value the mirror stores. 1e-6 degrees is
# about 0.11 m at the equator, well under any edit a publisher makes to a
# footprint.
_MIRROR_COORD_TOL = 1e-6

# Timestamp agreement tolerance. A STAC datetime is an RFC 3339 string with
# arbitrary fractional precision, while a Parquet timestamp column stores one
# fixed unit, so a conformant mirror rounds the fraction away. A mirror left
# behind by an edited item is off by minutes at least.
_MIRROR_TIME_TOL = timedelta(seconds=1)

# RFC 3339: fromisoformat on Python 3.10 rejects the 'Z' suffix and any
# fraction that is not three or six digits, both of which the grammar allows.
_RFC3339 = re.compile(
    r"(?P<stamp>\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<offset>[Zz]|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class _MirrorRows:
    """The mirror columns the comparison reads, one entry per row.

    ``geometry``, ``datetimes``, and ``bboxes`` are None when the file carries
    no such column, which is not the same as a column full of nulls.
    """

    ids: list[Any]
    geometry: list[Any] | None
    datetimes: list[Any] | None
    bboxes: list[Any] | None


def _check_mirror(
    node: Node, key: str, located: Locator, graph: CatalogGraph | None
) -> list[DataDefect]:
    """A mirror MUST reproduce the collection's items (PORTO-FMT-042).

    formats.md, Raster § Item mirror: "the file MUST reproduce the
    collection's items exactly at publish time — one row per item, each row
    carrying that item's fields." Five comparisons stand in for that: the row
    count against the item count, the two id sets against each other, and the
    geometry, datetime, and bbox of every matched row against the item it
    names. Geometry and datetime are what a client queries the mirror on, and
    they are what goes stale when an item is edited and the mirror is not
    regenerated.

    Fidelity stops short of every field. stac-geoparquet is lossy by design in
    places — its ``docs/drawbacks.md`` names the
    defined-versus-undefined-versus-null distinction among item properties — so
    a field-by-field equality rule would fault conformant files.

    Skipped when the graph is unavailable, when the node is not a collection,
    when the collection's items carry no ids to join on, or when it has no
    items at all. That last case is a structural defect rather than a
    row-level one, and ``PTL-COL-005`` owns it: a mirror on an item-less
    collection is reported there, from metadata alone, without the data extra
    and without reading the Parquet bytes.
    """
    if graph is None or node.kind != "collection":
        return []
    items = graph.items_of(node)
    if not items:
        return []  # PTL-COL-005 owns the item-less collection
    identified = {item.id: item for item in items if isinstance(item.id, str) and item.id}
    if not identified:
        return []  # unidentified items; PTL-STR/PTL-GEN own that gap

    try:
        source: Any = located.open_binary() if located.is_remote else located.source
        parquet = pq.ParquetFile(source)
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return []

    if "id" not in parquet.schema_arrow.names:
        return [
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' has no 'id' column, so its rows cannot be matched"
                " to the collection's items",
                key,
            )
        ]

    rows = _read_mirror_rows(parquet)
    if rows is None:
        return []
    defects = _mirror_membership_defects(key, identified, rows)
    defects.extend(_mirror_field_defects(key, identified, rows))
    return defects


def _mirror_membership_defects(
    key: str, identified: dict[str, Node], rows: _MirrorRows
) -> list[DataDefect]:
    """One row per item, and the two id sets equal.

    The counts are compared as counts because the id sets are sets: two rows
    sharing one id satisfy set equality, and a mirror that repeats an item is
    not one row per item.
    """
    defects: list[DataDefect] = []
    if len(rows.ids) != len(identified):
        defects.append(
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' holds {len(rows.ids)} row(s) for {len(identified)} "
                "item(s); the mirror must carry one row per item",
                key,
            )
        )
    present = {value for value in rows.ids if isinstance(value, str)}
    missing = sorted(set(identified) - present)
    extra = sorted(present - set(identified))
    parts = []
    if missing:
        parts.append(f"{len(missing)} item(s) absent from the mirror ({_sample(missing)})")
    if extra:
        parts.append(f"{len(extra)} mirror row(s) with no item ({_sample(extra)})")
    if parts:
        defects.append(
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' disagrees with the collection's items: " + ", ".join(parts),
                key,
            )
        )
    return defects


def _mirror_field_defects(
    key: str, identified: dict[str, Node], rows: _MirrorRows
) -> list[DataDefect]:
    """Geometry, datetime, and bbox of each matched row against its item.

    Only ids both sides carry are compared; an id on one side alone is the
    membership defect's business. A column the file lacks is left to the rule
    that owns it: the GeoParquet rules own a file with no geometry, and
    ``PTL-DAT-007`` owns one with no bbox covering column. A missing datetime
    column has no other owner, so it is reported here.
    """
    index = {value: i for i, value in enumerate(rows.ids) if isinstance(value, str)}
    off: dict[str, list[str]] = {"geometry": [], "datetime": [], "bbox": []}
    for item_id in sorted(set(identified) & set(index)):
        item, i = identified[item_id], index[item_id]
        if rows.geometry is not None and not _geometry_agrees(item, rows.geometry[i]):
            off["geometry"].append(item_id)
        if rows.datetimes is not None and not _datetime_agrees(item, rows.datetimes[i]):
            off["datetime"].append(item_id)
        if rows.bboxes is not None and not _bbox_agrees(item, rows.bboxes[i]):
            off["bbox"].append(item_id)
    defects = [
        DataDefect(
            DAT_MIRROR,
            Severity.ERROR,
            f"mirror asset '{key}' {field} disagrees with the item it names for "
            f"{len(ids)} item(s) ({_sample(ids)})",
            key,
        )
        for field, ids in off.items()
        if ids
    ]
    if rows.datetimes is None and any(_item_datetime(item) for item in identified.values()):
        defects.append(
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' has no 'datetime' column, so it does not carry the "
                "items' datetimes",
                key,
            )
        )
    return defects


def _read_mirror_rows(parquet: Any) -> _MirrorRows | None:
    """Read id, geometry, datetime, and bbox in one pass over the file."""
    names = parquet.schema_arrow.names
    geo = _geo_metadata(parquet) or {}
    primary = geo.get("primary_column")
    geometry_name = primary if isinstance(primary, str) and primary in names else None
    if geometry_name is None and "geometry" in names:
        geometry_name = "geometry"
    datetime_name = "datetime" if "datetime" in names else None
    bbox = _bbox_struct(parquet, geo)
    wanted = [geometry_name, datetime_name, bbox[0] if bbox is not None else None]
    try:
        table = parquet.read(columns=["id", *(name for name in wanted if name is not None)])
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return None
    return _MirrorRows(
        ids=table.column("id").to_pylist(),
        geometry=table.column(geometry_name).to_pylist() if geometry_name else None,
        datetimes=table.column(datetime_name).to_pylist() if datetime_name else None,
        bboxes=_bbox_column_values(table, bbox) if bbox is not None else None,
    )


def _bbox_struct(parquet: Any, geo: dict[str, Any]) -> tuple[str, list[str]] | None:
    """The covering column as a struct name and its four leaf field names.

    Read from the same ``covering`` declaration the row-group statistics use
    (:func:`_covering_bboxes`) rather than from a guessed column name. A
    covering nested deeper than one struct level yields None: statistics name
    their leaves by path, while a per-row read needs the struct itself.
    """
    columns = geo.get("columns")
    entry = columns.get(geo.get("primary_column")) if isinstance(columns, dict) else None
    covering = entry.get("covering") if isinstance(entry, dict) else None
    bbox = covering.get("bbox") if isinstance(covering, dict) else None
    if not isinstance(bbox, dict):
        return None
    paths = [bbox.get(corner) for corner in ("xmin", "ymin", "xmax", "ymax")]
    if not all(isinstance(path, list) and len(path) == 2 for path in paths):
        return None
    roots = {str(path[0]) for path in paths}  # type: ignore[index]
    if len(roots) != 1:
        return None
    root = roots.pop()
    if root not in parquet.schema_arrow.names:
        return None
    return root, [str(path[1]) for path in paths]  # type: ignore[index]


def _bbox_column_values(table: Any, bbox: tuple[str, list[str]]) -> list[Any]:
    """Per-row [xmin, ymin, xmax, ymax], or None for a row that carries none."""
    root, fields = bbox
    values: list[Any] = []
    for row in table.column(root).to_pylist():
        corners = [row.get(field) for field in fields] if isinstance(row, dict) else []
        numeric = [float(corner) for corner in corners if isinstance(corner, (int, float))]
        values.append(numeric if len(numeric) == 4 else None)
    return values


def _geometry_agrees(item: Node, blob: Any) -> bool:
    """Does one mirror row's geometry reproduce the item's?

    Both sides are built as shapely geometries, the mirror's from WKB and the
    item's from GeoJSON, and compared with ``shapely.equals_exact`` within
    ``_MIRROR_COORD_TOL``. That comparison is structural: it holds only when
    the two agree on type, on part and ring nesting, and on vertex order, so a
    polygon whose rings were redrawn over the same points is drift rather than
    a match. The tolerance is there because both sides hold float64, so exact
    equality would fault a mirror built from full-precision sources against
    item JSON written to a fixed number of decimals. A blob shapely cannot
    decode is left alone, because an encoding it does not know is not evidence
    of drift; a null geometry against an item that has one is drift.
    """
    declared = _shape(item.data.get("geometry"))
    if declared is None:
        return True  # a null or unreadable item geometry is nothing to compare against
    if blob is None:
        return False
    if not isinstance(blob, bytes):
        return True
    actual = _from_wkb(blob)
    if actual is None:
        return True
    return bool(shapely.equals_exact(declared, actual, tolerance=_MIRROR_COORD_TOL))


def _shape(geometry: Any) -> BaseGeometry | None:
    """A GeoJSON geometry mapping as a shapely geometry, or None if unusable.

    An empty geometry counts as unusable: a member-less collection or a
    coordinate-less point carries nothing to compare a mirror row against.
    """
    if not isinstance(geometry, dict):
        return None
    try:
        built = shape(geometry)
    except (ShapelyError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return None if built.is_empty else built


def _from_wkb(blob: bytes) -> BaseGeometry | None:
    """A WKB blob as a shapely geometry, or None if shapely cannot read it."""
    try:
        return from_wkb(blob)
    except (ShapelyError, TypeError, ValueError):
        return None


def _datetime_agrees(item: Node, value: Any) -> bool:
    """Does one mirror row's timestamp reproduce the item's ``datetime``?

    Both sides are normalized to UTC before the comparison, so a mirror
    written in local time with its offset still agrees with a ``Z`` item. An
    item whose ``datetime`` is null carries the interval in
    ``start_datetime``/``end_datetime`` instead and has nothing to compare.
    """
    declared = _item_datetime(item)
    if declared is None:
        return True
    if isinstance(value, str):
        value = _parse_timestamp(value)
    if not isinstance(value, datetime):
        return False
    return abs(_as_utc(value) - declared) <= _MIRROR_TIME_TOL


def _bbox_agrees(item: Node, value: Any) -> bool:
    """Does one mirror row's covering box reproduce the item's ``bbox``?"""
    declared = _as_bbox(item.data.get("bbox"))
    if declared is None:
        return True  # no declared bbox is nothing to compare against
    if not isinstance(value, list) or len(value) != len(declared):
        return False
    return all(abs(a - b) <= _MIRROR_COORD_TOL for a, b in zip(declared, value, strict=True))


def _item_datetime(item: Node) -> datetime | None:
    properties = item.data.get("properties")
    raw = properties.get("datetime") if isinstance(properties, dict) else None
    return _parse_timestamp(raw) if isinstance(raw, str) else None


def _parse_timestamp(value: str) -> datetime | None:
    """An RFC 3339 timestamp as an aware UTC datetime, or None if unparseable."""
    match = _RFC3339.match(value.strip())
    if match is None:
        return None
    fraction = (match["fraction"] or "").ljust(6, "0")[:6]
    offset = (match["offset"] or "Z").replace("Z", "+00:00").replace("z", "+00:00")
    if len(offset) == 5:  # +HHMM, which fromisoformat wants as +HH:MM
        offset = f"{offset[:3]}:{offset[3:]}"
    try:
        return _as_utc(datetime.fromisoformat(f"{match['stamp']}.{fraction}{offset}"))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    """A naive timestamp is read as UTC: stac-geoparquet writes the column so."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _check_vector_columns(
    node: Node, key: str, asset: dict[str, Any], located: Locator
) -> list[DataDefect]:
    """The table-extension SHOULDs for a GeoParquet data asset.

    formats.md, Vector Data: a vector collection SHOULD carry ``table:columns``
    on the collection itself (PORTO-FMT-046), and an item that carries a
    GeoParquet data asset SHOULD carry the same field in its ``properties``
    (PORTO-FMT-047). Without it a client discovers a hundred attribute columns
    only by reading the Parquet footer. Plain Parquet is the tabular SHOULD's
    business, and a partitioned collection has no data asset to reach here, so
    its columns are checked wherever its partition files are described.
    """
    if "data" not in _asset_roles(asset):
        return []
    try:
        source: Any = located.open_binary() if located.is_remote else located.source
        parquet = pq.ParquetFile(source)
    except Exception:  # noqa: BLE001 - unreadable Parquet: format/checksum checks own it
        return []
    if _geo_metadata(parquet) is None:
        return []  # plain Parquet: the Tabular Data SHOULDs own it
    if _has_table_columns(node, asset):
        return []
    where = (
        "the item does not document its columns in properties"
        if node.kind == "item"
        else "the collection does not document its columns"
    )
    return [
        DataDefect(
            DAT_VECTOR_COLUMNS,
            Severity.WARNING,
            f"vector asset '{key}': {where} with the table extension (table:columns)",
            key,
        )
    ]


def _asset_roles(asset: dict[str, Any]) -> list[str]:
    raw = asset.get("roles")
    if not isinstance(raw, list):
        return []
    return [role for role in raw if isinstance(role, str)]


def _has_table_columns(node: Node, asset: dict[str, Any]) -> bool:
    """Whether ``table:columns`` is declared anywhere the extension allows.

    The extension defines two placements, the object and the asset, and the
    reference catalog carries it on the collection. An item's fields live under
    ``properties``, which is where PORTO-FMT-047 asks for it. A per-asset
    declaration is the PORTO-FMT-048 escape for a collection whose data assets
    describe differing schemas, so it satisfies the collection SHOULD too.
    """
    holders = [node.data, asset]
    properties = node.data.get("properties")
    if isinstance(properties, dict):
        holders.append(properties)
    for holder in holders:
        columns = holder.get("table:columns")
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
    key: str, parquet: Any, geo: dict[str, Any], *, subject: str | None = None
) -> tuple[list[tuple[float, float, float, float]] | None, list[DataDefect]]:
    """Per-row-group [minx, miny, maxx, maxy] boxes, plus statistics defects.

    formats.md:64 accepts two satisfiers: a 1.1 ``bbox`` covering column whose
    leaf fields carry Parquet min/max, or — for GeoParquet 2.x / Parquet
    ``GEOMETRY`` — native ``GeospatialStatistics`` per row group. Neither
    source yielding a box for every row group is the MUST failure (ERROR).
    Native statistics alone still satisfy the MUST, but formats.md keeps the
    covering column "RECOMMENDED even where native statistics exist, since it
    adds page-level min/max stats that enable finer-grained pruning" — that
    SHOULD surfaces as a WARNING.
    """
    subject = subject or f"asset '{key}'"
    boxes = _covering_bboxes(parquet, geo)
    if boxes is not None:
        return boxes, []
    boxes = _native_bboxes(parquet, geo)
    if boxes is None:
        return None, [
            DataDefect(
                DAT_ROWGROUP_STATS,
                Severity.ERROR,
                f"{subject} provides no per-row-group spatial statistics "
                "(no bbox covering column with min/max stats, nor native GeospatialStatistics)",
                key,
            )
        ]
    return boxes, [
        DataDefect(
            DAT_ROWGROUP_STATS,
            Severity.WARNING,
            f"{subject} relies on native GeospatialStatistics without a bbox "
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


def _row_ordering_defects(
    key: str,
    parquet: Any,
    geo: dict[str, Any],
    rows: int,
    groups: int,
    *,
    report_unreadable: bool,
    subject: str | None = None,
) -> list[DataDefect]:
    """Check row order, whatever the file's row-group layout.

    formats.md:30 requires spatially ordered rows in every file, and a validator
    checks that by splitting them into the groups a conforming writer would have
    produced. How the file happens to be chunked does not change whether the rows
    are ordered, only how cheaply a reader can tell, so this runs at any count.

    Below ``_MIN_ORDERING_ROW_GROUPS`` it is the only check available. The
    reference stac-geoparquet writer shows why that matters. With no schema
    passed, ``parse_stac_items_to_arrow`` returns one contiguous record batch at
    any item count and ``to_parquet`` writes one row group per batch, leaving the
    row-group checks a single box to compare, which passes on any row order.

    ``report_unreadable`` is off when the row-group checks also run, since an
    unreadable covering column then leaves row order checked, not unchecked.
    """
    subject = subject or f"asset '{key}'"
    if rows < _ORDERING_CHUNKS * _MIN_CHUNK_ROWS:
        return []  # too few rows for a chunk's box to describe anything
    row_boxes = _row_bboxes(parquet, geo)
    # Re-apply the floor to the boxes that survived, not to the row count. Rows
    # without geometry carry no covering box, so a file can hold enough rows and
    # still leave too few boxes for a chunk's box to describe anything. Below the
    # floor the chunks hold at most one box each, which measures nothing: one box
    # leaves ``_is_spatially_ordered`` no consecutive pair to divide by, and a
    # handful yields a verdict drawn from a sample far under the floor.
    if row_boxes is None or len(row_boxes) < _ORDERING_CHUNKS * _MIN_CHUNK_ROWS:
        if not report_unreadable:
            return []
        if row_boxes is None:
            why = "with no bbox covering column to read"
        else:
            measured = len(row_boxes)
            verb = "carries" if measured == 1 else "carry"
            why = f"of which {measured} {verb} a covering box"
        return [
            DataDefect(
                DAT_ORDERING,
                Severity.INFO,
                f"{subject} holds {rows} rows in {_plural(groups, 'row group')} "
                f"{why}, so spatial ordering could not be evaluated",
                key,
            )
        ]
    if _rows_are_locally_grouped(_chunked_bboxes(row_boxes)):
        return []
    return [
        DataDefect(
            DAT_ORDERING,
            Severity.ERROR,
            f"{subject} rows are not spatially ordered: {rows} rows in "
            f"{_plural(groups, 'row group')} do not cluster spatially, so a reader "
            "cannot skip any part of the file",
            key,
        )
    ]


def _footer_proves_row_ordering(
    parquet: Any,
    geo: dict[str, Any],
    bboxes: list[tuple[float, float, float, float]],
    row_counts: list[int],
) -> bool:
    """Can row-group bounds prove that the synthetic row chunks pass?

    Each synthetic chunk gets the union of every row group it intersects. That
    box can be larger than the chunk's exact box, but never smaller. Passing
    with these conservative boxes therefore proves that the exact boxes pass.

    This shortcut requires a complete covering column. Native statistics do
    not expose per-row null counts, and null rows change the synthetic chunk
    boundaries after :func:`_row_bboxes` removes them.
    """
    covering_boxes = _covering_bboxes(parquet, geo)
    if covering_boxes != bboxes or not _covering_has_no_nulls(parquet, geo):
        return False
    chunk_boxes = _conservative_chunk_bboxes(bboxes, row_counts)
    # The row rule's own predicate, not the footer's: this shortcut exists to
    # settle the ROW check without reading rows, so it has to prove the thing
    # that check would have asked.
    return len(chunk_boxes) >= _MIN_ORDERING_ROW_GROUPS and _rows_are_locally_grouped(chunk_boxes)


def _covering_has_no_nulls(parquet: Any, geo: dict[str, Any]) -> bool:
    """Do all four covering leaves have known zero null counts?"""
    primary = geo.get("primary_column")
    columns = geo.get("columns")
    if not isinstance(columns, dict):
        return False
    covering = columns.get(primary, {}).get("covering", {}).get("bbox")
    if not isinstance(covering, dict):
        return False
    try:
        paths = [".".join(covering[corner]) for corner in ("xmin", "ymin", "xmax", "ymax")]
    except (KeyError, TypeError):
        return False
    meta = parquet.metadata
    index = _column_index(meta)
    if not all(path in index for path in paths):
        return False
    for i in range(meta.num_row_groups):
        group = meta.row_group(i)
        try:
            if any(group.column(index[path]).statistics.null_count != 0 for path in paths):
                return False
        except AttributeError:
            return False
    return True


def _conservative_chunk_bboxes(
    bboxes: list[tuple[float, float, float, float]], row_counts: list[int]
) -> list[tuple[float, float, float, float]]:
    """Bound each synthetic row chunk with the row groups it intersects."""
    rows = sum(row_counts)
    if rows == 0:
        return []
    size = -(-rows // _ORDERING_CHUNKS)
    group_ranges: list[tuple[int, int, tuple[float, float, float, float]]] = []
    group_start = 0
    for count, bbox in zip(row_counts, bboxes, strict=True):
        group_ranges.append((group_start, group_start + count, bbox))
        group_start += count
    chunks = []
    for start in range(0, rows, size):
        end = min(start + size, rows)
        intersecting = [bbox for left, right, bbox in group_ranges if left < end and right > start]
        if intersecting:
            chunks.append(_bbox_union(intersecting))
    return chunks


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _chunked_bboxes(
    row_boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Box each contiguous chunk of rows, as row groups would have been.

    Partitioning reads FMT-006's row-group criteria as a measurement method
    rather than as the requirement, which the spec implies but does not say.
    portolan-spec#100 records the ambiguity: both tests compare row groups, so
    a file with one group has no stated evaluation.
    """
    size = -(-len(row_boxes) // _ORDERING_CHUNKS)
    return [_bbox_union(row_boxes[i : i + size]) for i in range(0, len(row_boxes), size)]


def _row_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float]] | None:
    """Per-row [minx, miny, maxx, maxy] from the bbox covering column's values.

    Reads the four covering leaves and not the geometry, so the cost is four
    float64 columns. Returns None when the file has no 1.1 covering column, or
    when its leaves sit deeper than the single struct level the spec uses.

    Rows whose covering values are null are skipped rather than abandoning the
    file. GeoParquet permits a null geometry, writers give those rows a null
    covering box, and a row with no geometry has no position — so it cannot be
    out of spatial order, and one of them must not cost the whole file its
    ordering check. Returns None only when no row has a box at all.
    """
    columns = geo.get("columns")
    if not isinstance(columns, dict):
        return None
    covering = columns.get(geo.get("primary_column"), {}).get("covering", {}).get("bbox")
    if not isinstance(covering, dict):
        return None
    paths = [covering.get(corner) for corner in ("xmin", "ymin", "xmax", "ymax")]
    if not all(isinstance(p, list) and len(p) == 2 for p in paths):
        return None
    if len({p[0] for p in paths}) != 1:  # type: ignore[index]
        return None
    try:
        flat = parquet.read(columns=[paths[0][0]]).flatten()  # type: ignore[index]
        corners = [flat.column(f"{p[0]}.{p[1]}").to_pylist() for p in paths]  # type: ignore[index]
    except Exception:  # noqa: BLE001 - unreadable column: the checksum check owns bad bytes
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for corner_values in zip(*corners, strict=True):
        if any(value is None for value in corner_values):
            continue  # geometry-less row: no position, so nothing to order
        boxes.append(
            (
                float(corner_values[0]),
                float(corner_values[1]),
                float(corner_values[2]),
                float(corner_values[3]),
            )
        )
    return boxes or None


def _ideal_grid_boxes(
    extent: tuple[float, float, float, float], count: int
) -> list[tuple[float, float, float, float]]:
    """The best layout this box count allows: a near-square tiling of the extent.

    The reference the actual layout is judged against, so that "ordered" means
    "as good as this row-group count allows" rather than a fixed figure that only
    holds at one count. A grid is what a perfect space-filling-curve sort
    converges to, and it is generous towards clustered data, which cannot tile
    evenly — hence a threshold of half the achievable rate rather than all of it.
    """
    if count <= 0:
        return []
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    width = (extent[2] - extent[0]) / cols
    height = (extent[3] - extent[1]) / rows
    boxes = []
    for i in range(count):
        row, col = divmod(i, cols)
        boxes.append(
            (
                extent[0] + col * width,
                extent[1] + row * height,
                extent[0] + (col + 1) * width,
                extent[1] + (row + 1) * height,
            )
        )
    return boxes


def _mean_skip_rate(
    boxes: list[tuple[float, float, float, float]],
    windows: list[tuple[float, float, float, float]],
) -> float:
    """Mean fraction of boxes a reader can skip across the sample query windows."""
    if not boxes:
        return 0.0
    return sum(
        sum(1 for b in boxes if not _bbox_overlaps(w, b)) / len(boxes) for w in windows
    ) / len(windows)


def _sample_windows(
    extent: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    """Reproducible query windows spanning ``_QUERY_FRACTION`` of each dimension."""
    rng = random.Random(_QUERY_SEED)  # noqa: S311  # nosec B311 - sampling, not security
    width = (extent[2] - extent[0]) * _QUERY_FRACTION
    height = (extent[3] - extent[1]) * _QUERY_FRACTION
    windows = []
    for _ in range(_QUERY_SAMPLES):
        x = rng.uniform(extent[0], extent[2] - width)  # noqa: S311
        y = rng.uniform(extent[1], extent[3] - height)  # noqa: S311
        windows.append((x, y, x + width, y + height))
    return windows


def _rows_are_locally_grouped(bboxes: list[tuple[float, float, float, float]]) -> bool:
    """True if each synthetic row chunk covers a small part of the extent.

    The row rule (formats.md:30) asks whether nearby features are nearby in the
    file, which is not the same question as whether a reader can skip row groups.
    Rows scattered within two far-apart bands still let a reader skip half the
    file, so a pruning test passes them; their chunk boxes each span half the
    extent, so this one does not.

    A flat limit is right here because :data:`_ORDERING_CHUNKS` is constant: the
    rows are always split the same number of ways, so the reference never moves.
    """
    extent = _bbox_union(bboxes)
    extent_area = _bbox_area(extent)
    if extent_area == 0:
        return True  # a single location — nothing to order
    mean_ratio = sum(_bbox_area(b) for b in bboxes) / len(bboxes) / extent_area
    return mean_ratio < _MAX_LOCALITY_RATIO


def _is_spatially_ordered(bboxes: list[tuple[float, float, float, float]]) -> bool:
    """True if this layout prunes as well as its box count allows (formats.md:38).

    The footer check. Callers must hold the applicability guard themselves; with
    a single box there is nothing to skip past.

    Deliberately not the fraction of consecutive pairs that overlap
    (PORTO-FMT-049): row groups produced by a space-filling-curve sort are
    spatially adjacent by construction, so their boxes touch. For perfectly tiled
    data that fraction runs about 0.75 at thirteen boxes, 0.88 at fifty-nine and
    0.96 at five hundred and eighty-nine — it is near 1.0 for the best possible
    file, and cannot separate boxes that each span the extent from boxes that
    tile it.
    """
    extent = _bbox_union(bboxes)
    if _bbox_area(extent) == 0:
        return True  # a single location — nothing to order

    windows = _sample_windows(extent)
    achievable = _mean_skip_rate(_ideal_grid_boxes(extent, len(bboxes)), windows)
    if achievable <= 0:
        return True  # no layout could skip anything here — nothing to fall short of
    return _mean_skip_rate(bboxes, windows) / achievable >= _MIN_SKIP_EFFICIENCY


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


def _partition_files(node: Node) -> list[tuple[str, Path]]:
    """Local files the collection's ``partition:glob`` matches, sorted.

    The glob is expanded against the collection's own directory. A remote or
    absolute pattern (``s3://``, ``https://``, ``/data``) cannot be listed from
    the local tree, and one that normalizes outside the catalog is not the
    catalog's to read, so both yield nothing. Each entry pairs the relative
    match, which names the file in a message, with its path on disk.
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
    return [
        (match, base / match) for match in sorted(globmodule.glob(normalized, root_dir=str(base)))
    ]


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
    schemas: list[tuple[str, dict[str, str]]] = []
    for match, path in _partition_files(node):
        try:
            parquet = pq.ParquetFile(str(path))
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


def _check_partition_geoparquet(node: Node, reader: AssetReader) -> list[DataDefect]:
    """The GeoParquet MUSTs apply to partition files, not only to assets.

    A partitioned collection addresses its data by ``partition:glob`` and
    carries no ``data`` asset, which is the layout formats.md:113 prescribes.
    The per-asset loop therefore never reaches those files, leaving
    ``PTL-DAT-006``, ``007``, ``008``, and ``012`` unenforced on the only
    bytes the collection publishes. This runs the same checks over every
    matched file.

    A collection can hold hundreds of partitions written by one job, so a
    single bad setting would report hundreds of near-identical errors. Each
    rule reports once, naming how many files failed and quoting one of them.
    """
    files = _partition_files(node)
    if not files:
        return []
    already_read = _asset_paths(node, reader)
    found: list[DataDefect] = []
    checked = 0
    for match, path in files:
        if str(path) in already_read:
            continue  # a declared asset: the per-asset loop reports it
        checked += 1
        found.extend(
            _check_geoparquet(
                match,
                Locator(is_remote=False, source=str(path)),
                subject=f"partition file '{match}'",
            )
        )
    return _fold_partition_defects(found, checked)


def _asset_paths(node: Node, reader: AssetReader) -> set[str]:
    """Local paths the per-asset loop already reads on this node.

    A glob wide enough to match a declared asset would otherwise fault the same
    file twice, once per pass.
    """
    paths: set[str] = set()
    for _key, asset in _assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            continue
        located = reader.locate(node, href)
        if located is not None and not located.is_remote:
            paths.add(located.source)
    return paths


def _fold_partition_defects(defects: list[DataDefect], total: int) -> list[DataDefect]:
    """One defect per rule and severity, bound to the glob rather than an asset.

    Order follows first appearance, so the report reads in the order the files
    were walked. A lone failure keeps its own message; several keep the first
    as the example, since partitions of one dataset fail the same way.
    """
    groups: dict[tuple[str, Severity], list[DataDefect]] = {}
    for defect in defects:
        groups.setdefault((defect.rule_id, defect.severity), []).append(defect)
    folded: list[DataDefect] = []
    for (rule_id, severity), members in groups.items():
        first = members[0]
        message = first.message
        if len(members) > 1:
            message = (
                f"{len(members)} of {total} partition files fail this check; e.g. {first.message}"
            )
        folded.append(
            DataDefect(
                rule_id,
                severity,
                message,
                "",
                json_pointer="/partition:glob",
            )
        )
    return folded


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


def _declared_epsg(node: Node, asset: dict[str, Any]) -> _EpsgDeclaration | None:
    """Find the ``proj:epsg`` an asset is governed by, and where it is written.

    The asset's own value wins, then the enclosing object's ``properties``, then
    its document root. The two outer sources are inherited: one field governs
    every asset on the object, and that field is the one an editor changes.
    """
    sources: tuple[tuple[Any, str | None], ...] = (
        (asset, None),
        (node.data.get("properties", {}), "/properties/proj:epsg"),
        (node.data, "/proj:epsg"),
    )
    for source, pointer in sources:
        if isinstance(source, dict):
            value = source.get("proj:epsg")
            if isinstance(value, int) and not isinstance(value, bool):
                return _EpsgDeclaration(value, pointer)
    return None
