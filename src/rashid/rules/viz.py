"""Visualization rules (spec: core.md, Visualization; formats.md, PMTiles).

Metadata-checkable subset only: thumbnail presence, style assets when a
visualization derivative exists, and PMTiles registration. Whether a
render-from-source path is genuinely viable (a "small" GeoParquet, a
display-ready COG) is not decidable from metadata, so its absence is at
most an INFO nudge, never an error.

The thumbnail rule and the render-path nudge bind geospatial collections
only, and metadata does not always say which a collection is. Where the
answer decides whether a finding fires, the undecidability is itself
reported, softened one severity, so a publisher reading a clean run never
mistakes "could not tell" for "nothing to fix".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import links_of, roles_of
from rashid.rules.assets import _assets_of

PMTILES_MEDIA_TYPE = "application/vnd.pmtiles"
STYLE_MEDIA_TYPE = "application/vnd.mapbox.style+json"
_THUMBNAIL_TYPES = ("image/png", "image/jpeg", "image/webp")
_WEB_MAP_LINKS_PREFIX = "https://stac-extensions.github.io/web-map-links/"
# Render-from-source is plausible for small files; above this, a missing
# visual derivative is worth a nudge. Deliberately conservative.
_LARGE_VECTOR_BYTES = 100_000_000


def _column_declarations(node: Node) -> list[dict[str, Any]] | None:
    """Every ``table:columns`` entry describing the collection's own data.

    The table extension allows the field on the collection and on the asset
    that holds the table, and the spec's reference catalog puts it on the data
    asset, so both are read. Assets carrying the ``source`` role are skipped:
    an upstream CSV's columns describe what was converted, not what the
    collection publishes. None means nothing declared any, which is what
    separates "no geometry column" from "no column list".
    """
    holders: list[dict[str, Any]] = [node.data]
    holders += [asset for _p, _k, asset in _assets_of(node) if "source" not in roles_of(asset)]
    declared: list[dict[str, Any]] = []
    found = False
    for holder in holders:
        columns = holder.get("table:columns")
        if isinstance(columns, list):
            found = True
            declared += [column for column in columns if isinstance(column, dict)]
    return declared if found else None


def _names_a_geometry_column(columns: list[dict[str, Any]]) -> bool:
    for column in columns:
        name = str(column.get("name", "")).casefold()
        ctype = str(column.get("type", "")).casefold()
        if name in ("geometry", "geom") or "geometry" in ctype:
            return True
    return False


def _has_spatial_media_type(node: Node) -> bool:
    """Whether any asset's media type is spatial whatever the metadata says."""
    for _pointer, _key, asset in _assets_of(node):
        media_type = asset.get("type")
        if isinstance(media_type, str) and (
            media_type == PMTILES_MEDIA_TYPE
            or media_type.startswith("image/tiff")
            or media_type == "application/vnd.laszip+copc"
        ):
            return True
    return False


def is_geospatial(node: Node, graph: CatalogGraph) -> bool | None:
    """Best-effort geospatial detection from metadata alone.

    The spec identifies a tabular collection by its Parquet data having no
    geometry column — a data-pass fact. From metadata we can only look for
    positive signals: an item with a geometry, an inherently spatial media
    type, or a geometry column declared via the table extension. A declared
    column list with no geometry in it is the one negative signal, and it
    yields to a spatial media type, since a COG stays raster however an
    attribute table beside it is described. With no signal either way, return
    None: callers report that uncertainty rather than guess past it.
    """
    for child in graph.children_of(node):
        if child.kind == "item" and child.data.get("geometry") is not None:
            return True
    if _has_spatial_media_type(node):
        return True
    columns = _column_declarations(node)
    if columns is not None:
        return _names_a_geometry_column(columns)
    return None


def _undecided_because(node: Node, graph: CatalogGraph) -> str:
    """The signals that were absent, phrased as one clause each.

    Only called where :func:`is_geospatial` returned None, so all three
    signals are known to be missing; the item clause reports which of the two
    ways that happened, since publishing items and giving them geometries are
    separate edits.
    """
    items = sum(1 for child in graph.children_of(node) if child.kind == "item")
    if items == 0:
        seen = "it has no items"
    elif items == 1:
        seen = "its one item declares no geometry"
    else:
        seen = f"none of its {items} items declares a geometry"
    return (
        f"{seen}, neither it nor its assets declare table:columns, and no asset"
        " carries an inherently spatial media type (PMTiles, COG, or COPC)"
    )


#: Imperative repair for an undecidable collection, shared by both callers.
_RESOLVE_HINT = (
    "declare table:columns on the data asset, naming the geometry column when the"
    " data has one, so the next run can decide"
)


def _pmtiles_registration(node: Node) -> tuple[bool, bool]:
    """(has_pmtiles_asset, has_pmtiles_link) for a collection."""
    has_asset = any(
        asset.get("type") == PMTILES_MEDIA_TYPE
        or (isinstance(asset.get("href"), str) and str(asset["href"]).endswith(".pmtiles"))
        for _p, _k, asset in _assets_of(node)
    )
    has_link = any(link.get("rel") == "pmtiles" for link in links_of(node))
    return has_asset, has_link


class ThumbnailRule(Rule):
    """Every geospatial collection carries a thumbnail asset.

    A collection whose geospatial nature metadata cannot decide gets a WARNING
    naming the missing signals, not silence: PORTO-CORE-067 is a MUST, and a
    skipped MUST that reads as a pass hides the gap from the publisher. The
    warning is limited to collections carrying no thumbnail at all, because a
    collection that carries one satisfies the requirement whichever way the
    question would have resolved.
    """

    id = "PTL-VIZ-001"
    spec_ids = ("PORTO-CORE-067", "PORTO-FMT-033")
    default_severity = Severity.ERROR
    description = "geospatial collections must include a thumbnail asset (png or jpeg)"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        geospatial = is_geospatial(node, graph)
        if geospatial is False:
            return
        thumbnails = [
            (pointer, key, asset)
            for pointer, key, asset in _assets_of(node)
            if "thumbnail" in roles_of(asset)
        ]
        if geospatial is None:
            if not thumbnails:
                yield self.finding(
                    node,
                    "collection has no asset with the 'thumbnail' role, and whether it is"
                    " geospatial cannot be decided from metadata:"
                    f" {_undecided_because(node, graph)}",
                    severity=Severity.WARNING,
                    json_pointer="/assets",
                    fix_hint=f"{_RESOLVE_HINT}; add a thumbnail if the collection is geospatial",
                )
            return
        if not thumbnails:
            yield self.finding(
                node,
                "geospatial collection has no asset with the 'thumbnail' role",
                json_pointer="/assets",
                fix_hint="add a thumbnail generated from the collection's default styling",
            )
            return
        for pointer, key, asset in thumbnails:
            media_type = asset.get("type")
            if media_type not in _THUMBNAIL_TYPES:
                yield self.finding(
                    node,
                    f"thumbnail asset '{key}' has type {media_type!r}, expected image/png,"
                    " image/jpeg or image/webp",
                    json_pointer=f"{pointer}/type",
                    fix_hint="set the thumbnail's type to the media type of the file it points"
                    " at, 'image/png', 'image/jpeg' or 'image/webp'",
                    expected=list(_THUMBNAIL_TYPES),
                    actual=media_type,
                )


class StylesForDerivativeRule(Rule):
    """A collection with a visualization derivative registers style assets.

    Self-rendering collections (no derivative) are exempt: whether the data
    asset is display-ready is not decidable from metadata.
    """

    id = "PTL-VIZ-002"
    spec_ids = ("PORTO-CORE-067", "PORTO-CORE-068", "PORTO-CORE-069", "PORTO-FMT-014")
    default_severity = Severity.ERROR
    description = "collections with a visual derivative must register style assets"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        has_visual = any("visual" in roles_of(asset) for _p, _k, asset in _assets_of(node))
        has_pmtiles_asset, has_pmtiles_link = _pmtiles_registration(node)
        if not (has_visual or has_pmtiles_asset or has_pmtiles_link):
            return
        if not any("style" in roles_of(asset) for _p, _k, asset in _assets_of(node)):
            yield self.finding(
                node,
                "collection has a visualization derivative but no asset with the 'style' role",
                json_pointer="/assets",
                fix_hint="register the MapLibre style in styles/ as a collection-level"
                ' asset with roles ["style"]',
            )


class PMTilesRegistrationRule(Rule):
    """A provided PMTiles file is registered per web-map-links."""

    id = "PTL-VIZ-003"
    spec_ids = ("PORTO-FMT-011", "PORTO-FMT-012")
    default_severity = Severity.ERROR
    description = "PMTiles must be registered via a rel:'pmtiles' link (web-map-links v1.3.0)"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        has_asset, has_link = _pmtiles_registration(node)
        if not has_asset and not has_link:
            return
        if has_asset and not has_link:
            yield self.finding(
                node,
                "PMTiles asset is not registered through a rel:'pmtiles' link",
                json_pointer="/links",
                fix_hint='add {"rel": "pmtiles", "href": <the PMTiles href>, "type":'
                f' "{PMTILES_MEDIA_TYPE}", "pmtiles:layers": [<default-visible layers>]}}'
                " to links",
            )
            return
        for index, link in enumerate(links_of(node)):
            if link.get("rel") != "pmtiles":
                continue
            if link.get("type") != PMTILES_MEDIA_TYPE:
                yield self.finding(
                    node,
                    f"rel:'pmtiles' link has type {link.get('type')!r},"
                    f" expected '{PMTILES_MEDIA_TYPE}'",
                    json_pointer=f"/links/{index}/type",
                    fix_hint=f"set the link's type to '{PMTILES_MEDIA_TYPE}'",
                    expected=PMTILES_MEDIA_TYPE,
                    actual=link.get("type"),
                )
            layers = link.get("pmtiles:layers")
            if not isinstance(layers, list) or not layers:
                yield self.finding(
                    node,
                    "rel:'pmtiles' link has no pmtiles:layers array of default-visible layers",
                    json_pointer=f"/links/{index}",
                    fix_hint="add pmtiles:layers to the link, naming the tile layers a client"
                    ' should show by default, e.g. ["roads"]',
                )
        extensions = node.data.get("stac_extensions")
        declared = isinstance(extensions, list) and any(
            isinstance(uri, str) and uri.startswith(_WEB_MAP_LINKS_PREFIX) for uri in extensions
        )
        if not declared:
            yield self.finding(
                node,
                "rel:'pmtiles' link used without declaring the web-map-links extension"
                " schema in stac_extensions",
                json_pointer="/stac_extensions",
                fix_hint=f"add '{_WEB_MAP_LINKS_PREFIX}v1.3.0/schema.json' to stac_extensions",
            )


class StyleMediaTypeRule(Rule):
    """Style assets in a PMTiles collection carry the MapLibre media type.

    formats.md, PMTiles: "For PMTiles the style is a MapLibre GL style file
    (MapLibre GL style spec v8) in a `styles/` subdirectory, with media type
    `application/vnd.mapbox.style+json`". Scoped to collections that provide
    PMTiles, where the spec pins the style format; the style's presence is
    PTL-VIZ-002's finding.
    """

    id = "PTL-VIZ-005"
    spec_ids = ("PORTO-FMT-015",)
    default_severity = Severity.ERROR
    description = (
        "style assets in PMTiles collections must be typed application/vnd.mapbox.style+json"
    )
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        has_asset, has_link = _pmtiles_registration(node)
        if not has_asset and not has_link:
            return
        for pointer, key, asset in _assets_of(node):
            if "style" not in roles_of(asset):
                continue
            media_type = asset.get("type")
            if media_type != STYLE_MEDIA_TYPE:
                yield self.finding(
                    node,
                    f"style asset '{key}' has type {media_type!r}, expected '{STYLE_MEDIA_TYPE}'",
                    json_pointer=f"{pointer}/type",
                    fix_hint=f"set the style asset's type to '{STYLE_MEDIA_TYPE}'",
                    expected=STYLE_MEDIA_TYPE,
                    actual=media_type,
                )


class DefaultStyleRoleRule(Rule):
    """A collection with multiple styles marks its default with the `default` role.

    core.md, Visualization Styles: "when a collection provides more than one
    style, exactly one style asset MUST carry both `style` and `default` in its
    `roles`". STAC assets are an unordered JSON object whose keys carry no
    meaning a client is expected to understand, so the default can be read off
    neither position nor key; the second role names it. A lone style is the
    default implicitly, so this only bites when more than one is registered.
    """

    id = "PTL-VIZ-006"
    spec_ids = ("PORTO-CORE-070",)
    default_severity = Severity.ERROR
    description = "collections with multiple styles must mark the default with the 'default' role"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        styles = [(key, asset) for _p, key, asset in _assets_of(node) if "style" in roles_of(asset)]
        if len(styles) < 2:
            return
        marked = sorted(key for key, asset in styles if "default" in roles_of(asset))
        if len(marked) == 1:
            return
        message = (
            f"collection registers {len(styles)} style assets but none carries the 'default'"
            " role, so the default style cannot be identified"
            if not marked
            else f"collection marks {len(marked)} style assets with the 'default' role"
            f" ({', '.join(marked)}); exactly one is the default"
        )
        yield self.finding(
            node,
            message,
            json_pointer="/assets",
            fix_hint="add 'default' to the roles of exactly one style asset",
        )


class LargeVectorWithoutVisualRule(Rule):
    """Large vector data without a visual derivative gets a nudge.

    The spec requires a zero-infrastructure render path; render-from-source
    is only plausible for small files. The size threshold is heuristic, so
    this is INFO, never an error.

    A collection whose geospatial nature metadata cannot decide gets the same
    nudge worded as the uncertainty it is, still INFO: a large Parquet file
    needs a visual derivative only if it holds geometries, and nothing here
    establishes that it does.
    """

    id = "PTL-VIZ-004"
    spec_ids = ("PORTO-CORE-066",)
    default_severity = Severity.INFO
    description = "large vector collections likely need a visual derivative (PMTiles)"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        geospatial = is_geospatial(node, graph)
        if geospatial is False:
            return
        has_visual = any("visual" in roles_of(asset) for _p, _k, asset in _assets_of(node))
        has_pmtiles_asset, has_pmtiles_link = _pmtiles_registration(node)
        if has_visual or has_pmtiles_asset or has_pmtiles_link:
            return
        for _pointer, key, asset in _assets_of(node):
            if "data" not in roles_of(asset):
                continue
            if asset.get("type") != "application/vnd.apache.parquet":
                continue
            size = asset.get("file:size")
            if not isinstance(size, int) or isinstance(size, bool) or size <= _LARGE_VECTOR_BYTES:
                continue
            if geospatial is None:
                yield self.finding(
                    node,
                    f"data asset '{key}' is {size} bytes with no visual derivative, and"
                    " whether this collection is geospatial cannot be decided from"
                    f" metadata: {_undecided_because(node, graph)}",
                    json_pointer="/assets",
                    fix_hint=f"{_RESOLVE_HINT}; publish a PMTiles derivative with a MapLibre"
                    " style if the collection is geospatial",
                )
                continue
            yield self.finding(
                node,
                f"vector data asset '{key}' is {size} bytes with no visual"
                " derivative; rendering from source is unlikely to be viable",
                json_pointer="/assets",
                fix_hint="publish a PMTiles derivative with a MapLibre style",
            )
