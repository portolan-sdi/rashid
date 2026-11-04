"""Shared helpers for rule modules."""

from __future__ import annotations

import os.path
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlsplit

from rashid._href import is_absolute_href, object_location
from rashid.catalog import CatalogGraph, Node
from rashid.remote import wire_url

# Rels that make the tree navigable; every one carries type requirements and
# must be relative and resolvable.
STRUCTURAL_RELS = ("root", "parent", "child", "item", "collection")

PMTILES_MEDIA_TYPE = "application/vnd.pmtiles"

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


def is_pmtiles_asset(asset: dict[str, Any]) -> bool:
    """Identify PMTiles by media type or href, including URL query strings."""
    href = asset.get("href")
    return asset.get("type") == PMTILES_MEDIA_TYPE or (
        isinstance(href, str) and urlsplit(href).path.lower().endswith(".pmtiles")
    )


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


def asset_refers_to_self(node: Node, href: str, graph: CatalogGraph | None = None) -> bool:
    """Compare an asset href with the containing object's known locations."""
    if not href:
        return False
    if os.path.normcase(os.path.normpath(href)) == os.path.normcase(str(node.abs_path)):
        return True
    # Path.as_uri() imports the network stack on Python 3.14.
    path = node.abs_path.as_posix()
    local = "file://" + ("" if path.startswith("/") else "/") + quote(path, safe="/:")
    if not is_absolute_href(href):
        if graph is not None and graph.base_url is not None:
            location = wire_url(graph.base_url, str(node.path))
            return object_location(urljoin(location, href)) == object_location(location)
        # Relative filesystem paths are literal filenames, not encoded URLs.
        return object_location(urljoin(local, quote(href, safe="/:"))) == object_location(local)
    locations = [local]
    locations.extend(
        link["href"]
        for link in links_of(node)
        if link.get("rel") == "self"
        and isinstance(link.get("href"), str)
        and is_absolute_href(link["href"])
    )
    if graph is not None:
        locations.extend(
            wire_url(base, str(node.path))
            for base in (graph.base_url, graph.published_base())
            if base is not None
        )
    return any(
        object_location(urljoin(location, href)) == object_location(location)
        for location in locations
    )


def names_a_geometry_column(columns: list[dict[str, Any]]) -> bool:
    for column in columns:
        name = str(column.get("name", "")).casefold()
        ctype = str(column.get("type", "")).casefold()
        if name in ("geometry", "geom") or "geometry" in ctype:
            return True
    return False


def declares_geometry(
    node: Node, asset: dict[str, Any] | None = None, collection: Node | None = None
) -> bool | None:
    """Read table geometry evidence from the node, its properties, and its assets.

    With an asset, read only that asset and its owner/collection declarations;
    otherwise read all non-source assets. Source columns describe upstream
    data rather than what the collection publishes. A ``table:primary_geometry``
    name or a geometry column takes precedence over a negative column list.
    None means neither a primary geometry nor a column list was declared.
    """
    holders: list[dict[str, Any]] = [node.data]
    properties = node.data.get("properties")
    if isinstance(properties, dict):
        holders.append(properties)
    if asset is None:
        assets = node.data.get("assets")
        if isinstance(assets, dict):
            holders += [
                value
                for value in assets.values()
                if isinstance(value, dict) and "source" not in roles_of(value)
            ]
    else:
        holders.append(asset)
    if collection is not None and collection is not node:
        holders.append(collection.data)
    declared: list[dict[str, Any]] = []
    found = False
    for holder in holders:
        primary = holder.get("table:primary_geometry")
        if isinstance(primary, str) and primary:
            return True
        columns = holder.get("table:columns")
        if isinstance(columns, list):
            found = True
            declared += [column for column in columns if isinstance(column, dict)]
    return names_a_geometry_column(declared) if found else None
