"""Shared helpers for rule modules."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from rashid.catalog import Node

# Rels that make the tree navigable; every one carries type requirements and
# must be relative and resolvable.
STRUCTURAL_RELS = ("root", "parent", "child", "item", "collection")

# core.md, Assets: Portolan raster data is COG, declared with this media type.
# A plain GeoTIFF may appear only as an upstream source, not as primary data.
COG_MEDIA_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_TIFF_MEDIA_TYPE = "image/tiff"
_COG_PARAMETERS = {"application=geotiff", "profile=cloud-optimized"}

# The filename suffixes a raster scene carries. Public because scene files
# are also counted straight off the directory listing, where no asset
# declares a media type to read.
TIFF_SUFFIXES = (".tif", ".tiff")


def links_of(node: Node) -> list[dict[str, Any]]:
    """The node's links array, tolerating a missing or malformed field."""
    raw = node.data.get("links")
    if not isinstance(raw, list):
        return []
    return [link for link in raw if isinstance(link, dict)]


def roles_of(asset: dict[str, Any]) -> list[str]:
    """The asset's roles, tolerating a missing or malformed field."""
    raw = asset.get("roles")
    if not isinstance(raw, list):
        return []
    return [role for role in raw if isinstance(role, str)]


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


def is_raster_data_asset(asset: dict[str, Any]) -> bool:
    """Whether an asset declares primary raster-data intent.

    Applicability cannot depend on a correct COG declaration: doing that lets
    a bad media type hide the COG media-type, scene-modeling, and item-mirror
    findings it should trigger. The data role plus either a TIFF type or TIFF
    href establishes raster intent. Upstream source assets are deliberately
    excluded because the spec permits them to remain ordinary GeoTIFFs.
    """
    roles = roles_of(asset)
    if "data" not in roles or "source" in roles:
        return False
    media_type = asset.get("type")
    if (
        isinstance(media_type, str)
        and media_type.split(";", 1)[0].strip().lower() == _TIFF_MEDIA_TYPE
    ):
        return True
    href = asset.get("href")
    if not isinstance(href, str):
        return False
    return urlparse(href).path.lower().endswith(TIFF_SUFFIXES)


def parse_rfc3339(value: object) -> datetime | None:
    """Parse an RFC 3339 date-time; None when invalid or offset-less."""
    if not isinstance(value, str):
        return None
    normalized = value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
