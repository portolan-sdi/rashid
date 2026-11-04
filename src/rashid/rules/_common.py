"""Shared helpers for rule modules."""

from __future__ import annotations

import os.path
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from rashid._href import (
    is_absolute_href,
    local_path,
    object_location,
    parse_href,
    split_href,
    wire_url,
)
from rashid.catalog import CatalogGraph, Node
from rashid.model import json_pointer

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


def indexed_links_of(node: Node) -> Iterator[tuple[int, dict[str, Any]]]:
    """The node's valid link objects with their original array positions."""
    raw = node.data.get("links")
    for index, link in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(link, dict):
            yield index, link


def links_of(node: Node) -> list[dict[str, Any]]:
    """The node's links array, tolerating a missing or malformed field."""
    return [link for _index, link in indexed_links_of(node)]


def roles_of(asset: dict[str, Any]) -> list[str]:
    """The asset's roles, tolerating a missing or malformed field."""
    raw = asset.get("roles")
    if not isinstance(raw, list):
        return []
    return [role for role in raw if isinstance(role, str)]


def is_source_asset(asset: dict[str, Any]) -> bool:
    """Only the source role exempts upstream bytes from format requirements."""
    return "source" in roles_of(asset)


def assets_of(node: Node) -> list[tuple[str, str, dict[str, Any]]]:
    """(pointer_prefix, asset_key, asset) triples in declaration order."""
    assets = node.data.get("assets")
    if not isinstance(assets, dict):
        return []
    return [
        (json_pointer("assets", key), key, asset)
        for key, asset in assets.items()
        if isinstance(asset, dict)
    ]


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
    if "data" not in roles_of(asset) or is_source_asset(asset):
        return False
    if is_tiff_media_type(asset.get("type")):
        return True
    href = asset.get("href")
    if not isinstance(href, str):
        return False
    parsed = parse_href(href)
    return parsed is not None and parsed.path.lower().endswith(TIFF_SUFFIXES)


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
    if not href or split_href(href) is None:
        return False
    if os.path.normcase(os.path.normpath(href)) == os.path.normcase(str(node.abs_path)):
        return True
    # Path.as_uri() imports the network stack on Python 3.14.
    path = node.abs_path.as_posix()
    local = "file://" + ("" if path.startswith("/") else "/") + quote(path, safe="/:")
    if not is_absolute_href(href):
        if graph is not None and graph.base_url is not None:
            location = wire_url(graph.base_url, str(graph.href_path(node)))
            return object_location(href, base=location) == object_location(location)
        # File identity follows the reader's decoded, suffix-free local href.
        return local_path(quote(str(node.path.parent), safe="/"), href) == str(node.path)
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
            wire_url(base, str(graph.href_path(node)))
            for base in (graph.base_url, graph.published_base())
            if base is not None
        )
    return any(
        (known := object_location(location)) is not None
        and object_location(href, base=location) == known
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
    declared: list[dict[str, Any]] = []
    found = False
    for holder in table_declarations(node, asset, collection):
        primary = holder.get("table:primary_geometry")
        if isinstance(primary, str) and primary:
            return True
        columns = holder.get("table:columns")
        if isinstance(columns, list):
            found = True
            declared += [column for column in columns if isinstance(column, dict)]
    return names_a_geometry_column(declared) if found else None


def table_declarations(
    node: Node, asset: dict[str, Any] | None = None, collection: Node | None = None
) -> Iterator[dict[str, Any]]:
    """Table metadata holders; no asset means all non-source assets.

    An explicit asset limits the declarations to that asset and its object.
    Geometry applicability also accepts empty column lists as negative evidence;
    column documentation requires a nonempty list instead.
    """
    holders: list[dict[str, Any]] = [node.data]
    properties = node.data.get("properties")
    if isinstance(properties, dict):
        holders.append(properties)
    if asset is None:
        holders += [
            value for _pointer, _key, value in assets_of(node) if not is_source_asset(value)
        ]
    else:
        holders.append(asset)
    if collection is not None and collection is not node:
        holders.append(collection.data)
    yield from holders


def has_table_columns(node: Node, asset: dict[str, Any]) -> bool:
    """A nonempty column declaration at object, properties, or asset level."""
    return any(
        isinstance(columns := holder.get("table:columns"), list) and bool(columns)
        for holder in table_declarations(node, asset)
    )
