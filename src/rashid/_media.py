"""Media types and asset format intent."""

from __future__ import annotations

from typing import Any, Literal

from rashid._href import split_href
from rashid._stac import assets_of, is_source_asset, roles_of
from rashid.catalog import Node

PMTILES_MEDIA_TYPE = "application/vnd.pmtiles"
PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
COPC_MEDIA_TYPE = "application/vnd.laszip+copc"
# incubating/zarr.md, Access: a Zarr store asset has the Zarr media type
# with a version parameter, and MAY include more parameters such as
# `profile=multiscales`. PTL-ZAR-004 judges the whole declaration.
ZARR_MEDIA_TYPE = "application/vnd.zarr"
# The filename suffix a Zarr store has, with or without a trailing slash.
ZARR_SUFFIX = ".zarr"
# Icechunk is its own format. Its media type is not a Zarr media type, and a
# repository is not a Zarr store unless it is served as plain Zarr v3 under a
# Zarr media type, so the word is refused wherever "zarr" is matched loosely.
_ICECHUNK_MARKER = "icechunk"
# Media types that say nothing about the format, so an href may speak instead.
_UNTYPED = frozenset({"", "application/octet-stream", "binary/octet-stream"})
# core.md, Assets: Portolan raster data is COG, declared with this media type.
# A plain GeoTIFF may appear only as an upstream source, not as primary data.
COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_TIFF_MEDIA_TYPE = "image/tiff"
_COG_PARAMETERS = {"application=geotiff", "profile=cloud-optimized"}
# The filename suffixes a raster scene has. Public because scene files
# are also counted straight off the directory listing, where no asset
# declares a media type to read.
TIFF_SUFFIXES = (".tif", ".tiff")


def media_type_essence(value: object) -> str | None:
    """A media type without its parameters, lowercased: ``text/html; charset=utf-8`` is ``text/html``."""
    if not isinstance(value, str):
        return None
    return value.split(";", 1)[0].strip().casefold()


def is_tiff_media_type(
    value: object, *, policy: Literal["base-type", "prefix", "literal-prefix"] = "base-type"
) -> bool:
    """TIFF intent under the metadata, byte-probe, or spatial-media policy.

    Primary raster intent matches the normalized base type. Byte probing
    retains its case-insensitive prefix match; spatial-media evidence retains
    its literal prefix match, independently of roles and hrefs.
    """
    if not isinstance(value, str):
        return False
    if policy == "literal-prefix":
        return value.startswith(_TIFF_MEDIA_TYPE)
    if policy == "prefix":
        return value.lower().startswith(_TIFF_MEDIA_TYPE)
    return value.split(";", 1)[0].strip().lower() == _TIFF_MEDIA_TYPE


def media_parameters(value: str) -> dict[str, str]:
    """The parameters of a media type, lowercased, order and spacing ignored."""
    parameters: dict[str, str] = {}
    for part in value.split(";")[1:]:
        name, separator, raw = part.partition("=")
        if separator:
            parameters[name.strip().lower()] = raw.strip().strip('"').lower()
    return parameters


def media_base_type(value: object) -> str:
    """A media type's base type, lowercased and trimmed; empty when there is none."""
    if not isinstance(value, str):
        return ""
    return value.split(";", 1)[0].strip().lower()


def names_a_zarr_store(value: object) -> bool:
    """Whether a media type names a Zarr store, however it is spelled.

    The profile requires ``application/vnd.zarr``, and catalogs in the wild
    write ``application/vnd+zarr``, ``application/x-zarr`` and
    ``application/zarr`` for the same thing. Reading the loose spellings as a
    store is what lets ``PTL-ZAR-004`` report the media type that is wrong; a
    strict match would make the misspelling hide the rule that governs it.
    Icechunk is excluded: its media type names a different format.
    """
    base = media_base_type(value)
    return "zarr" in base and _ICECHUNK_MARKER not in base


def is_zarr_store_asset(asset: dict[str, Any]) -> bool:
    """Whether an asset publishes a Zarr store, so the profile's rules bind it.

    The media type decides whenever it names a format at all. An asset that
    declares none, or declares only that it is bytes, falls back to its href:
    a ``data`` asset at a ``.zarr`` prefix is a store whatever it forgot to
    say. A media type that names some other format is believed rather than
    second-guessed, so a COG or a ``.zarr.zip`` archive is never read as a
    store. An upstream ``source`` asset is out of scope either way.
    """
    media_type = asset.get("type")
    if names_a_zarr_store(media_type):
        return True
    if is_source_asset(asset) or "data" not in roles_of(asset):
        return False
    if media_base_type(media_type) not in _UNTYPED:
        return False
    href = asset.get("href")
    parsed = split_href(href) if isinstance(href, str) else None
    return parsed is not None and parsed.path.rstrip("/").lower().endswith(ZARR_SUFFIX)


def is_cog_media_type(value: object) -> bool:
    """True when a media type names a cloud-optimized GeoTIFF.

    The TIFF base type and both required parameters matter. Parameter order,
    case, and surrounding whitespace do not; a missing parameter means the
    declaration does not name the COG media type Portolan requires.
    """
    if not isinstance(value, str):
        return False
    parts = [part.strip().lower() for part in value.split(";")]
    return bool(parts) and parts[0] == _TIFF_MEDIA_TYPE and _COG_PARAMETERS.issubset(parts[1:])


def is_partial_read_media_type(value: object) -> bool:
    """Whether clients read a file of this media type by byte range.

    core.md, Data Storage lists COG, Parquet, PMTiles, and COPC.
    """
    return is_cog_media_type(value) or value in (
        PARQUET_MEDIA_TYPE,
        PMTILES_MEDIA_TYPE,
        COPC_MEDIA_TYPE,
    )


def is_raster_data_asset(asset: dict[str, Any]) -> bool:
    """Whether an asset declares primary raster-data intent.

    Applicability cannot depend on a correct COG declaration: doing that lets
    a bad media type hide the COG media-type, scene-modeling, and item-mirror
    findings it should trigger. The data role plus either a TIFF type or TIFF
    href establishes raster intent. Upstream source assets are deliberately
    excluded because the spec permits them to remain ordinary GeoTIFFs.
    """
    if "data" not in roles_of(asset) or is_source_asset(asset):
        return False
    if is_tiff_media_type(asset.get("type")):
        return True
    href = asset.get("href")
    if not isinstance(href, str):
        return False
    parsed = split_href(href)
    return parsed is not None and parsed.path.lower().endswith(TIFF_SUFFIXES)


def is_overview_asset(asset: dict[str, Any]) -> bool:
    """formats.md, Raster: an overview is a COG with the visual and overview roles.

    The ``overview`` role alone is STAC's generic preview image, often a PNG.
    """
    return {"visual", "overview"} <= set(roles_of(asset)) and is_cog_media_type(asset.get("type"))


def has_cog(node: Node) -> bool:
    """Whether any asset declares a COG, regardless of its roles or href."""
    return any(is_cog_media_type(asset.get("type")) for _pointer, _key, asset in assets_of(node))


def is_pmtiles_asset(asset: dict[str, Any]) -> bool:
    """Identify PMTiles by media type or href, including URL query strings."""
    href = asset.get("href")
    parsed = split_href(href) if isinstance(href, str) else None
    return asset.get("type") == PMTILES_MEDIA_TYPE or (
        parsed is not None and parsed.path.lower().endswith(".pmtiles")
    )
