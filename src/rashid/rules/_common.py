"""Shared helpers for rule modules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rashid.catalog import Node

# Rels that make the tree navigable; every one carries type requirements and
# must be relative and resolvable.
STRUCTURAL_RELS = ("root", "parent", "child", "item", "collection")

# core.md, Assets: the required media type for a COG is
# "image/tiff; application=geotiff; profile=cloud-optimized". The profile
# parameter is part of that type. Matching the bare image/tiff prefix would
# also count a plain GeoTIFF.
_COG_MEDIA_PREFIX = "image/tiff"
_COG_MEDIA_PROFILE = "profile=cloud-optimized"


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

    Both halves of the type matter. ``image/tiff`` alone is a plain GeoTIFF,
    and the ``profile=cloud-optimized`` parameter is what makes it a COG.
    Checking only the prefix counts every GeoTIFF as cloud-optimized.
    Checking only the profile accepts the parameter on a non-TIFF type.
    """
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.startswith(_COG_MEDIA_PREFIX) and _COG_MEDIA_PROFILE in normalized


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
