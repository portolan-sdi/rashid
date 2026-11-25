"""STAC document fields, asset roles, and hrefs."""

from __future__ import annotations

import os.path
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeGuard
from urllib.parse import quote

from rashid_core._href import (
    is_absolute_href,
    is_uri_reference,
    local_path,
    object_location,
    split_href,
    wire_url,
)
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.model import json_pointer

# Rels that make the tree navigable; every one has type requirements and
# must be relative and resolvable.
STRUCTURAL_RELS = ("root", "parent", "child", "item", "collection")


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


def _is_https_url(href: object) -> bool:
    """A well-formed https URL with a host and a valid port."""
    if not isinstance(href, str) or not is_uri_reference(href):
        return False
    parsed = split_href(href)
    if parsed is None or parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return True


def https_alternate(asset: dict[str, Any]) -> str | None:
    """The first https URL in the asset's ``alternate`` field (PORTO-CORE-087)."""
    alternates = asset.get("alternate")
    if not isinstance(alternates, dict):
        return None
    for alt in alternates.values():
        if isinstance(alt, dict) and _is_https_url(alt.get("href")):
            return str(alt["href"])
    return None


def read_target(asset: dict[str, Any]) -> str | None:
    """The href a client reads the asset through.

    A relative or https href as declared; for another scheme, such as ``s3``,
    the https alternate that PORTO-CORE-087 requires; None when there is none.
    """
    href = asset.get("href")
    if not isinstance(href, str) or not href:
        return None
    parsed = split_href(href)
    if parsed is not None and parsed.scheme.lower() in ("", "https"):
        return href
    return https_alternate(asset)


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
        names = ("geometry", "geom", "geography", "geog")
        if name in names or "geometry" in ctype or "geography" in ctype:
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


MIRROR_ROLE = "collection-mirror"
MIRROR_FILENAME = "items.parquet"


def mirror_assets(node: Node) -> list[tuple[str, str, dict[str, object]]]:
    """The node's item-mirror assets, found by role or href.

    Role is the normative signal, but a producer that gets the role wrong
    still means the asset as a mirror, and PTL-MIR-002 should say so rather
    than treat it as an unrelated Parquet file. The filename is the other way
    the same intent shows up. The asset key has no signal: the
    stac-geoparquet spec names none, so the producer chooses it.
    """
    return [entry for entry in assets_of(node) if is_mirror_asset(entry[2])]


def is_mirror_asset(asset: dict[str, object]) -> bool:
    """Whether one asset is an item mirror rather than collection data.

    The data pass uses this to route a mirror through its agreement check
    (``PORTO-FMT-042``) on top of the GeoParquet requirements that bind it
    like any other spatial table (``PORTO-FMT-043``).
    """
    href = asset.get("href")
    return MIRROR_ROLE in roles_of(asset) or (
        isinstance(href, str) and href.rsplit("/", 1)[-1] == MIRROR_FILENAME
    )


# Version-tolerant prefix of the partition extension's schema URI, matching
# how the web-map-links declaration is checked in the viz rules.
PARTITION_EXTENSION_PREFIX = "https://schemas.portolan-sdi.org/incubating/partition/"


def is_partitioned(node: Node) -> bool:
    return any(key.startswith("partition:") for key in node.data) or bool(
        _declared_extensions(node)
    )


def _declared_extensions(node: Node) -> list[tuple[int, str]]:
    """``(index, uri)`` of each partition extension URI in ``stac_extensions``."""
    extensions = node.data.get("stac_extensions")
    if not isinstance(extensions, list):
        return []
    return [
        (index, uri)
        for index, uri in enumerate(extensions)
        if isinstance(uri, str) and uri.startswith(PARTITION_EXTENSION_PREFIX)
    ]


def providers_of(node: Node) -> list[dict[str, Any]]:
    raw = node.data.get("providers")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def bboxes_of(node: Node) -> list[tuple[str, Any]]:
    """All bbox arrays on a node, with a JSON pointer for each."""
    found: list[tuple[str, Any]] = []
    if node.kind == "item":
        if "bbox" in node.data:
            found.append((json_pointer("bbox"), node.data["bbox"]))
        return found
    extent = node.data.get("extent")
    if isinstance(extent, dict):
        spatial = extent.get("spatial")
        if isinstance(spatial, dict):
            boxes = spatial.get("bbox")
            if isinstance(boxes, list):
                for index, box in enumerate(boxes):
                    found.append((json_pointer("extent", "spatial", "bbox", index), box))
    return found


def has_datetime(fields: dict[str, Any]) -> bool:
    """Whether ``fields`` include a datetime or both ends of an interval."""
    return fields.get("datetime") is not None or (
        fields.get("start_datetime") is not None and fields.get("end_datetime") is not None
    )


CLASSES_FIELD = "classification:classes"
BAND_FIELDS = ("bands", "raster:bands")
_ABSENT = object()


@dataclass(frozen=True)
class ClassList:
    """One ``classification:classes`` array and the band facts that bind it.

    ``nodata`` is the band's own value, or the enclosing asset's when the
    band leaves it out, as STAC 1.1 inherits it. ``_ABSENT`` marks a value
    declared at neither level. ``band_field`` and
    ``band_index`` locate the band in its asset's band list, so the data pass
    can pair it with the band of the same position in the file; both are None
    for a list declared outside a band list.
    """

    pointer: str
    classes: Any
    nodata: Any
    band_field: str | None = None
    band_index: int | None = None
    # On a source asset, which PORTO-FMT-045 exempts from Portolan's own
    # class requirements; the extension's class object still applies.
    source: bool = False

    def entries(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """(pointer, class object) for each class that is an object."""
        if not isinstance(self.classes, list):
            return
        for index, entry in enumerate(self.classes):
            if isinstance(entry, dict):
                yield json_pointer(index, prefix=self.pointer), entry


def asset_class_lists(pointer: str, asset: dict[str, Any]) -> list[ClassList]:
    """Every class list on one asset: its own, and each band's.

    A legacy ``raster:bands`` list is left out where it mirrors a list in
    ``bands`` or, for the asset's only band, on the asset.
    """
    nodata = asset.get("nodata", _ABSENT)
    found: list[ClassList] = []
    if CLASSES_FIELD in asset:
        found.append(
            ClassList(json_pointer(CLASSES_FIELD, prefix=pointer), asset[CLASSES_FIELD], nodata)
        )
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if not isinstance(bands, list):
            continue
        for index, band in enumerate(bands):
            if not isinstance(band, dict) or CLASSES_FIELD not in band:
                continue
            if field == "raster:bands" and _mirrors(asset, index, band):
                continue
            found.append(
                ClassList(
                    json_pointer(field, index, CLASSES_FIELD, prefix=pointer),
                    band[CLASSES_FIELD],
                    band.get("nodata", nodata),
                    field,
                    index,
                )
            )
    return found


def _home(asset: dict[str, Any], index: int) -> dict[str, Any] | None:
    """The ``bands`` entry, or the single-band asset, with classes for legacy band ``index``."""
    if _has_classes(asset, index):
        band: dict[str, Any] = asset["bands"][index]
        return band
    if index == 0 and band_count(asset) == 1 and CLASSES_FIELD in asset:
        return asset
    return None


def _mirrors(asset: dict[str, Any], index: int, legacy: dict[str, Any]) -> bool:
    """Whether a legacy ``raster:bands`` entry repeats the class list and nodata of its home."""
    home = _home(asset, index)
    if home is None or home.get(CLASSES_FIELD) != legacy[CLASSES_FIELD]:
        return False
    inherited = asset.get("nodata", _ABSENT)
    return bool(home.get("nodata", inherited) == legacy.get("nodata", inherited))


def is_integer(value: Any) -> TypeGuard[int]:
    """A JSON integer as draft-07 reads it: 1 or 1.0, but neither true nor 1.5."""
    # JSON true is a Python int; the schema's integer type excludes it.
    if isinstance(value, float):
        return value.is_integer()
    return isinstance(value, int) and not isinstance(value, bool)


def _has_classes(asset: dict[str, Any], index: int | None) -> bool:
    """Whether the asset (index None) or its band ``index`` in ``bands`` has classes."""
    if index is None:
        return CLASSES_FIELD in asset
    bands = asset.get("bands")
    if not isinstance(bands, list) or index >= len(bands):
        return False
    band = bands[index]
    return isinstance(band, dict) and CLASSES_FIELD in band


def band_count(asset: dict[str, Any]) -> int:
    """How many bands the asset declares; one when it lists none."""
    for field in BAND_FIELDS:
        bands = asset.get(field)
        if isinstance(bands, list):
            return len(bands)
    return 1
