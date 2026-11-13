"""COG structure, band statistics, and raster spatial metadata."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from fractions import Fraction
from typing import Any

import rasterio
from pyproj import CRS
from rasterio.enums import ColorInterp, MaskFlags
from rio_cogeo.cogeo import cog_validate

from rashid import _trace
from rashid.catalog import CatalogGraph, Node
from rashid.data import DataDefect
from rashid.data._files import AssetFiles
from rashid.data.reader import Locator
from rashid.data.spatial import Geo
from rashid.model import Severity, json_pointer
from rashid.registry import (
    DAT_CLASS_COLORS,
    DAT_COG,
    DAT_COG_STATS,
    DAT_OVERVIEW_FORMAT,
    DAT_OVERVIEWS,
    DAT_PALETTE_CLASSES,
    DAT_SCENE_MASK,
    DAT_TILE_SIZE,
    DAT_VALID_PERCENT,
)
from rashid.rules._common import is_overview_asset, roles_of
from rashid.rules.classification import (
    BAND_FIELDS,
    asset_class_lists,
    band_count,
    is_integer,
)

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


def check_raster(
    key: str, asset: dict[str, Any], located: Locator, files: AssetFiles
) -> list[DataDefect]:
    """A raster asset MUST be a valid COG (formats.md:91) with embedded band stats.

    ``cog_validate`` takes a path, not a dataset, so it opens the file itself.
    The other checks share the one dataset in ``files``.
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
        defects.extend(_check_class_placement(key, asset, files))
        defects.extend(_check_class_colors(key, asset, files))
        defects.extend(_check_nodata_classes(key, asset, files))
        defects.extend(_check_palette_classes(key, asset, files))
    return defects


def classified_asset(
    node: Node, key: str, asset: dict[str, Any], graph: CatalogGraph | None
) -> dict[str, Any]:
    """The asset whose class lists and bitfields describe this file.

    An item asset that declares neither inherits the ones its collection's
    ``item_assets`` template declares for the same key.
    """
    if _described_bands(asset) or node.kind != "item" or graph is None:
        return asset
    collection = graph.enclosing_collection_of(node)
    templates = collection.data.get("item_assets") if collection is not None else None
    template = templates.get(key) if isinstance(templates, dict) else None
    return template if isinstance(template, dict) and _described_bands(template) else asset


def _described_bands(asset: dict[str, Any]) -> set[int]:
    """Zero-based bands that classes or bitfields name; asset-level ones name band 0."""
    described = {
        0 if found.band_index is None else found.band_index
        for found in asset_class_lists("", asset)
    }
    # A bit-packed band names its values with classification:bitfields.
    if _BITFIELDS in asset:
        described.add(0)  # the asset's own bitfields describe its only band
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if isinstance(bands, list):
            described.update(
                index
                for index, band in enumerate(bands)
                if isinstance(band, dict) and _BITFIELDS in band
            )
    return described


def _drawn_classes(
    key: str, asset: dict[str, Any]
) -> Iterator[tuple[int | None, int, str, str | None]]:
    """(file band, class value, JSON pointer, hint) for each class a reader draws.

    A band list's classes describe the file band at the same position. An
    asset-level list has no band (None) and describes the asset's only band,
    so it is compared only against a single-band file. A nodata class is
    skipped, since its pixels draw transparent whatever the table says, and so
    is a malformed value, which ``PTL-CLS-002`` reports. A malformed hint
    becomes None for the same reason.
    """
    for found in asset_class_lists(json_pointer("assets", key), asset):
        band = None if found.band_index is None else found.band_index + 1
        for pointer, entry in found.entries():
            value, hint = entry.get("value"), entry.get("color_hint")
            if entry.get("nodata") is True or not is_integer(value):
                continue
            valid = hint.upper() if isinstance(hint, str) and _COLOR_HINT.match(hint) else None
            yield band, value, pointer, valid


_COLOR_HINT = re.compile(r"^[0-9A-Fa-f]{6}$")
_BITFIELDS = "classification:bitfields"


def _class_color_defect(  # noqa: PLR0913, PLR0917 - one drawn class against its file band
    key: str,
    band: int,
    value: int,
    pointer: str,
    hint: str | None,
    nodata: float | None,
    table: dict[int, tuple[int, ...]] | None,
) -> DataDefect | None:
    """The WARNING one drawn class earns against its band's nodata and color table, if any."""
    if nodata is not None and nodata == value:
        return DataDefect(
            DAT_CLASS_COLORS,
            Severity.WARNING,
            f"asset '{key}' class value {value} is band {band}'s nodata in the file,"
            " so readers hide it, but the class is not marked nodata",
            key,
            json_pointer=pointer,  # the class; its nodata flag may be absent
            expected=True,
        )
    if table is None:
        return None
    entry = table.get(value)
    if entry is None:
        return DataDefect(
            DAT_CLASS_COLORS,
            Severity.WARNING,
            f"asset '{key}' class value {value} has no entry in band {band}'s embedded"
            " color table, so readers cannot draw it",
            key,
            json_pointer=json_pointer("value", prefix=pointer),
        )
    if len(entry) > 3 and entry[3] == 0:
        return DataDefect(
            DAT_CLASS_COLORS,
            Severity.WARNING,
            f"asset '{key}' class value {value} is fully transparent in band {band}'s"
            " embedded color table, so readers hide it like nodata",
            key,
            json_pointer=pointer,  # the class; its nodata flag may be absent
            expected=True,
        )
    actual = "{:02X}{:02X}{:02X}".format(*entry[:3])
    if hint is None or actual == hint:
        return None
    return DataDefect(
        DAT_CLASS_COLORS,
        Severity.WARNING,
        f"asset '{key}' class value {value} has color_hint '{hint}', but band"
        f" {band}'s embedded color table draws it as '{actual}'",
        key,
        json_pointer=json_pointer("color_hint", prefix=pointer),
        expected=actual,
        actual=hint,
    )


def _color_table(src: Any, band: int) -> dict[int, tuple[int, ...]] | None:
    try:
        table: dict[int, tuple[int, ...]] = src.colormap(band)
    except ValueError:  # the band has no color table
        return None
    return table


_PICTURE = frozenset({ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha})


def _picture_defect(key: str, band: int, pointer: str) -> DataDefect:
    """The WARNING for a class list on one channel of an RGB(A) picture."""
    return DataDefect(
        DAT_CLASS_COLORS,
        Severity.WARNING,
        f"asset '{key}' band {band} is a color channel of a picture, so its pixels are"
        " intensities, not the classes it declares",
        key,
        json_pointer=pointer,
    )


def _check_class_colors(key: str, asset: dict[str, Any], files: AssetFiles) -> list[DataDefect]:
    """A class SHOULD agree with the COG band it describes.

    Each disagreement is a WARNING that the legend a client builds from STAC
    will not match the map.

    - A class list on a red, green, blue or alpha band describes one channel
      of a picture, whose pixels are intensities, not categories.
    - A class whose value is the file's nodata is masked by every reader, so
      it must be marked ``nodata`` to agree with the file.
    - On a band with an embedded color table (the TIFF ``ColorMap`` tag),
      which any TIFF reader draws and which therefore wins, a class the table
      cannot draw (a value past a short table), a class drawn fully
      transparent, and a ``color_hint`` whose red, green and blue differ from
      the table entry. Alpha is compared only for full transparency, since a
      hint carries none.
    """
    drawn = list(_drawn_classes(key, asset))
    if not drawn:
        return []
    try:
        src = files.raster()
        tables: dict[int, dict[int, tuple[int, ...]] | None] = {}
        pictures: dict[int, str] = {}
        defects: list[DataDefect] = []
        for band, value, pointer, hint in drawn:
            if band is None:
                band = 1 if src.count == 1 else None
            if band is None or band > src.count:
                continue
            if band not in tables:
                tables[band] = _color_table(src, band)
            if src.colorinterp[band - 1] in _PICTURE:
                # One finding per picture band, at the first class list on it.
                pictures.setdefault(band, pointer.rsplit("/", 1)[0])
                continue
            defect = _class_color_defect(
                key, band, value, pointer, hint, src.nodatavals[band - 1], tables[band]
            )
            if defect is not None:
                defects.append(defect)
        defects.extend(_picture_defect(key, band, pointer) for band, pointer in pictures.items())
    except Exception as exc:
        return [
            DataDefect(
                DAT_CLASS_COLORS,
                Severity.INFO,
                f"asset '{key}' color table could not be read ({exc})",
                key,
            )
        ]
    return defects


def _check_palette_classes(key: str, asset: dict[str, Any], files: AssetFiles) -> list[DataDefect]:
    """A band drawn through a color table may be classified without saying so.

    A palette band (an embedded TIFF ``ColorMap``) often holds categories, and
    without ``classification:classes`` a client can draw it but cannot say
    what any color means. A palette does not prove it, though (JRC water
    occurrence is a continuous percentage with one), and only the producer
    can declare a raster classified (PORTO-FMT-049), so this is an INFO hint.
    """
    described = _described_bands(asset)
    try:
        src = files.raster()
        palette = [
            index
            for index, interp in enumerate(src.colorinterp)
            if interp == ColorInterp.palette and index not in described
        ]
    except Exception:
        return []
    declared = asset.get("bands")
    declared = declared if isinstance(declared, list) else []
    return [
        DataDefect(
            DAT_PALETTE_CLASSES,
            Severity.INFO,
            f"asset '{key}' band {index + 1} draws through an embedded color table but"
            " declares no classification:classes",
            key,
            json_pointer=json_pointer("assets", key, "bands", index)
            # Point at the band only when the metadata lists it.
            if index < len(declared)
            else json_pointer("assets", key),
        )
        for index in palette
    ]


def _check_class_placement(key: str, asset: dict[str, Any], files: AssetFiles) -> list[DataDefect]:
    """A class list MUST be placed for the file's band count (PORTO-FMT-050).

    ``PTL-CLS-005`` judges placement by the band count the metadata declares.
    When that disagrees with the file, the file decides: a multi-band file
    needs the list on a band in ``bands``, a single-band file on the asset,
    and no list may describe a band the file lacks.
    """
    every = asset_class_lists(json_pointer("assets", key), asset)
    lists = [found for found in every if found.band_field != "raster:bands"]
    if not every:
        return []
    try:
        count = files.raster().count
    except Exception:  # unreadable raster: the COG check owns reporting it
        return []
    declared = band_count(asset)
    on_asset = [found for found in lists if found.band_index is None]
    on_band = [found for found in lists if found.band_index is not None]
    # A legacy raster:bands list, too, cannot describe a band the file lacks.
    beyond = [
        found for found in every if found.band_index is not None and found.band_index >= count
    ]
    # Where the metadata agrees with the file, PTL-CLS-005 reports misplacement.
    if count > 1 and on_asset and declared <= 1:
        where, misplaced = "on the asset instead of on the band it describes in bands", on_asset
    elif count == 1 and on_band and declared > 1:
        where, misplaced = "on a band instead of on the asset", on_band
    elif beyond:
        where, misplaced = f"on band {beyond[0].band_index} in bands, which the file lacks", beyond
    else:
        return []
    return [
        DataDefect(
            DAT_CLASS_COLORS,
            Severity.ERROR,
            f"asset '{key}' file has {count} band{'s' if count > 1 else ''},"
            f" but its class list is {where}",
            key,
            json_pointer=misplaced[0].pointer,
        )
    ]


def _check_nodata_classes(key: str, asset: dict[str, Any], files: AssetFiles) -> list[DataDefect]:
    """A class marked nodata SHOULD be the file's nodata (PORTO-FMT-057).

    ``PTL-CLS-003`` compares it with the metadata's nodata. When the file
    declares another nodata, readers draw the class the legend leaves out.
    """
    marked = [
        (found, pointer, entry["value"])
        for found in asset_class_lists(json_pointer("assets", key), asset)
        for pointer, entry in found.entries()
        if entry.get("nodata") is True and is_integer(entry.get("value"))
    ]
    if not marked:
        return []
    try:
        src = files.raster()
        count, nodatavals, masks = src.count, src.nodatavals, src.mask_flag_enums
    except Exception:  # unreadable raster: the COG check owns reporting it
        return []
    defects: list[DataDefect] = []
    for found, pointer, value in marked:
        band = 1 if found.band_index is None and count == 1 else None
        if found.band_index is not None and found.band_index < count:
            band = found.band_index + 1
        if band is None:
            continue  # PTL-DAT-021's placement check reports a band the file lacks
        nodata = nodatavals[band - 1]
        if nodata is None and masks[band - 1] == [MaskFlags.all_valid]:
            reason = "the file declares no nodata or mask for it"
        elif nodata is not None and nodata != value:
            reason = f"band {band}'s nodata in the file is {nodata:g}"
        else:
            continue
        defects.append(
            DataDefect(
                DAT_CLASS_COLORS,
                Severity.WARNING,
                f"asset '{key}' class value {value} is marked nodata, but {reason},"
                " so readers draw it",
                key,
                json_pointer=json_pointer("nodata", prefix=pointer),
                expected=nodata,
                actual=value,
            )
        )
    return defects


def from_item_assets(key: str, defects: list[DataDefect]) -> list[DataDefect]:
    """Relocate class findings for classes an item takes from ``item_assets``.

    The list is in the collection, not the item the finding is reported on,
    so the finding points at the item's asset, which exists, and says where
    the classes come from.
    """
    return [
        replace(
            defect,
            json_pointer=json_pointer("assets", key),
            message=f"{defect.message} (classes from the collection's item_assets/{key})",
        )
        if defect.rule_id in {DAT_CLASS_COLORS, DAT_PALETTE_CLASSES}
        else defect
        for defect in defects
    ]


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
    overview = node.kind == "collection" and is_overview_asset(asset)
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
