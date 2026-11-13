"""COG structure, band statistics, and raster spatial metadata."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import rasterio
from pyproj import CRS
from rasterio.enums import ColorInterp, MaskFlags
from rio_cogeo.cogeo import cog_validate

from rashid import _trace
from rashid.catalog import Node
from rashid.data import DataDefect
from rashid.data._files import AssetFiles
from rashid.data.reader import Locator
from rashid.data.spatial import Geo
from rashid.model import Severity
from rashid.registry import (
    DAT_COG,
    DAT_COG_STATS,
    DAT_OVERVIEW_FORMAT,
    DAT_OVERVIEWS,
    DAT_SCENE_MASK,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
)
from rashid.rules._common import roles_of

# geotiff-stats-headers.md — the embedded per-band statistics a COG MUST carry.
_COG_STAT_KEYS = (
    "STATISTICS_MINIMUM",
    "STATISTICS_MAXIMUM",
    "STATISTICS_MEAN",
    "STATISTICS_STDDEV",
)

# formats.md:121 — SHOULD per band, MUST when the band has a nodata value.
_VALID_PERCENT_KEY = "STATISTICS_VALID_PERCENT"

# STAC best-practice and EO roles for per-pixel masks: their zeros are values,
# not empty pixels, so such an asset is not an imagery scene.
_MASK_ROLES = {"data-mask", "date", "snow-ice", "land-water", "water-mask", "cloud", "cloud-shadow"}


def check_raster(key: str, located: Locator, files: AssetFiles) -> list[DataDefect]:
    """A raster asset MUST be a valid COG (formats.md:91) with embedded band stats.

    ``cog_validate`` takes a path, not a dataset, so it opens the file itself.
    The other three checks share the one dataset in ``files``.
    """
    defects: list[DataDefect] = []
    try:
        _trace.count("raster.cog_validates")
        is_valid, errors, _warnings = cog_validate(located.gdal_path(), quiet=True)
    except Exception as exc:  # a reader failure is not a conformance fault
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
    # GDAL_PAM_ENABLED=NO ignores any .aux.xml sidecar, so only what is
    # embedded in the file counts — as the spec requires.
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        defects.extend(_check_cog_stats(key, files))
        defects.extend(_check_overviews(key, files))
        defects.extend(_check_tile_size(key, files))
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


def _check_overviews(key: str, files: AssetFiles) -> list[DataDefect]:
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
        src = files.raster()
        shape = _tile_shape(src)
        tile_height, tile_width = shape if shape is not None else (_MAX_TILE, _MAX_TILE)
        tile = max(tile_height, tile_width)
        oversized = max(src.width, src.height) > tile
        factors = src.overviews(1)
        shapes = files.overview_shapes() if oversized and factors else []
    except Exception:  # unreadable raster: the COG check owns reporting it
        return []
    if not oversized:
        return []
    if not factors:
        reason = f"raster exceeds its own {tile}px internal tile but carries no internal overviews"
    elif any(
        not _overview_reduction(previous, shape)
        for previous, shape in zip([(src.height, src.width), *shapes[:-1]], shapes, strict=True)
    ):
        reason = f"overview pyramid factors {factors} must reduce each preceding level by 2–10"
    elif shapes[-1][1] > tile_width or shapes[-1][0] > tile_height:
        reason = f"overview pyramid factors {factors} stop before the coarsest level fits one {tile}px internal tile"
    else:
        return []
    return [DataDefect(DAT_OVERVIEWS, Severity.ERROR, f"asset '{key}' {reason}", key)]


def _overview_reduction(previous: tuple[int, int], current: tuple[int, int]) -> bool:
    """Can one scale in [2, 10] yield both dimensions after pixel rounding?"""
    if previous == current:
        return False
    dimensions = list(zip(previous, current, strict=True))
    scale = max(Fraction(2), *(Fraction(before, after) for before, after in dimensions))
    # ceil(before / scale) == after, with an exact bound at integer pixels.
    return scale <= 10 and all(before > (after - 1) * scale for before, after in dimensions)


def _check_tile_size(key: str, files: AssetFiles) -> list[DataDefect]:
    """Internal tiles MUST be square and no larger than 512×512.

    OGC 21-026 /req/optimized_geotiff/small-sizes ("square internal tiles,
    sized no larger than a common screen viewport. 512×512 is the usual
    choice", formats.md:121), checked directly rather than delegated to
    ``rio-cogeo``, which enforces no explicit bound. Untiled rasters are
    skipped: tiling itself is a base-COG requirement that ``cog_validate``
    already reports through PTL-DAT-004.
    """
    try:
        shape = _tile_shape(files.raster())
        if shape is None:
            return []
        tile_height, tile_width = shape
    except Exception:  # unreadable raster: the COG check owns reporting it
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


def _check_cog_stats(key: str, files: AssetFiles) -> list[DataDefect]:
    """Every COG band MUST carry embedded min/max/mean/stddev (formats.md:95),
    and SHOULD carry a valid percent — a MUST when the band has a nodata value
    (formats.md:121).

    Only statistics embedded in the file's GDAL_METADATA tag count, which is
    why :func:`check_raster` reads the dataset with PAM off.
    """
    try:
        src = files.raster()
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
    except Exception as exc:  # unreadable raster is advisory here
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


def check_display(
    node: Node, key: str, asset: dict[str, Any], files: AssetFiles
) -> list[DataDefect]:
    """formats.md, Raster: an overview is 8-bit RGB(A); an imagery scene masks empty pixels.

    An imagery scene is an item-level data COG with three or more bands and no
    mask role: a one-band DEM or index is not drawn as a photo, so it is not judged.
    Overview color interpretation is not compared: GDAL's COG driver writes
    three 8-bit bands as undefined unless PHOTOMETRIC=RGB is set.
    """
    roles = set(roles_of(asset))
    overview = node.kind == "collection" and "overview" in roles
    scene = node.kind == "item" and "data" in roles and not roles & _MASK_ROLES
    if not (overview or scene):
        return []
    try:
        with rasterio.Env(GDAL_PAM_ENABLED="NO"):
            src = files.raster()
            count, dtypes = src.count, sorted(set(src.dtypes))
            alpha = count == 4 and src.colorinterp[3] == ColorInterp.alpha
            masked = any(flags != [MaskFlags.all_valid] for flags in src.mask_flag_enums)
    except Exception:  # unreadable raster: the COG check owns reporting it
        return []
    if overview and (dtypes != ["uint8"] or not (count == 3 or alpha)):
        return [
            DataDefect(
                DAT_OVERVIEW_FORMAT,
                Severity.WARNING,
                f"overview asset '{key}' has {count} band(s) of {', '.join(dtypes)};"
                " an overview should be 8-bit RGB with an optional alpha band",
                key,
            )
        ]
    if scene and count >= 3 and not masked:
        return [
            DataDefect(
                DAT_SCENE_MASK,
                Severity.WARNING,
                f"imagery asset '{key}' declares no nodata value and has no alpha band or mask,"
                " so clients draw its empty pixels as black",
                key,
            )
        ]
    return []


def geo_from_raster(files: AssetFiles) -> Geo | None:
    # The dataset the COG checks share, read with PAM off as they read it.
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        src = files.raster()
        bounds = src.bounds
        crs = CRS.from_wkt(src.crs.to_wkt()) if src.crs else None
        return Geo(
            bbox=[bounds.left, bounds.bottom, bounds.right, bounds.top],
            epsg=crs.to_epsg() if crs else None,
            crs=crs,
            # the grid extent, which a nodata collar can hold well inside it
            tight=False,
        )
