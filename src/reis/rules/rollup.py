"""Item-rollup rules (spec: formats.md, Raster § Item rollup).

A raster collection that models scenes as items SHOULD publish a
stac-geoparquet rollup of those items at ``items.parquet``
(``PORTO-FMT-040``), and a published rollup MUST be registered twice on the
collection: as an asset keyed ``geoparquet-items`` with role ``stac-items``,
and as a ``rel: "items"`` link, both typed
``application/vnd.apache.parquet`` (``PORTO-FMT-041``).

Both checks read metadata only. Whether the rollup's rows match the
collection's items is a bytes question, so it lives in the data pass
(``PTL-DAT-016``).
"""

from __future__ import annotations

from collections.abc import Iterable

from reis.catalog import CatalogGraph, Node
from reis.model import Finding, Severity
from reis.rule import Rule
from reis.rules._common import links_of, roles_of
from reis.rules.assets import _assets_of

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
ROLLUP_ROLE = "stac-items"
ROLLUP_ASSET_KEY = "geoparquet-items"
ROLLUP_FILENAME = "items.parquet"
_COG_MEDIA_PREFIX = "image/tiff"
_COG_MEDIA_PROFILE = "profile=cloud-optimized"


def rollup_assets(node: Node) -> list[tuple[str, str, dict[str, object]]]:
    """The node's rollup assets, found by role, key, or href.

    Role is the normative signal, but a producer that gets the role wrong
    still means the asset as a rollup, and PTL-ROL-002 should say so rather
    than treat it as an unrelated Parquet file. The key and the filename are
    the other two ways the same intent shows up.
    """
    found = []
    for pointer, key, asset in _assets_of(node):
        href = asset.get("href")
        by_href = isinstance(href, str) and href.rsplit("/", 1)[-1] == ROLLUP_FILENAME
        if ROLLUP_ROLE in roles_of(asset) or key == ROLLUP_ASSET_KEY or by_href:
            found.append((pointer, key, asset))
    return found


def is_rollup_asset(asset: dict[str, object]) -> bool:
    """Whether one asset is an item rollup rather than collection data.

    The data pass uses this to exempt rollups from the GeoParquet data
    requirements (``PORTO-FMT-043``).
    """
    href = asset.get("href")
    return ROLLUP_ROLE in roles_of(asset) or (
        isinstance(href, str) and href.rsplit("/", 1)[-1] == ROLLUP_FILENAME
    )


def _items_links(node: Node) -> list[tuple[int, dict[str, object]]]:
    return [(i, link) for i, link in enumerate(links_of(node)) if link.get("rel") == "items"]


def _has_cog(node: Node) -> bool:
    for _pointer, _key, asset in _assets_of(node):
        media_type = asset.get("type")
        if not isinstance(media_type, str):
            continue
        normalized = media_type.strip().lower()
        if normalized.startswith(_COG_MEDIA_PREFIX) and _COG_MEDIA_PROFILE in normalized:
            return True
    return False


def _scene_items(node: Node, graph: CatalogGraph) -> list[Node]:
    return [child for child in graph.children_of(node) if child.kind == "item" and _has_cog(child)]


class ItemRollupPresentRule(Rule):
    """A raster collection with scene items publishes an items.parquet rollup.

    Scoped to the shape the spec names: items that carry COGs. A collection
    whose single COG sits at collection level has no items and owes no
    rollup, and a vector or point-cloud collection is outside the ratified
    SHOULD (those rollups are still incubating). SHOULD-level, so WARNING.
    """

    id = "PTL-ROL-001"
    spec_ids = ("PORTO-FMT-040",)
    default_severity = Severity.WARNING
    description = "raster collections with scene items should publish an items.parquet rollup"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        scenes = _scene_items(node, graph)
        if not scenes:
            return
        if rollup_assets(node) or _items_links(node):
            return
        yield self.finding(
            node,
            f"raster collection models {len(scenes)} scene(s) as items but publishes no"
            " stac-geoparquet rollup",
            json_pointer="/assets",
            fix_hint="write the items to items.parquet in the collection root, then register"
            f" it as the '{ROLLUP_ASSET_KEY}' asset with roles [\"{ROLLUP_ROLE}\"] and as a"
            " rel:'items' link",
        )


class ItemRollupRegistrationRule(Rule):
    """A published rollup carries both registrations, with the right role and type.

    Fires only once a rollup exists in some form. The asset reaches clients
    that read assets and the link reaches clients that walk links, so one
    without the other hides the rollup from half of them.
    """

    id = "PTL-ROL-002"
    spec_ids = ("PORTO-FMT-041",)
    default_severity = Severity.ERROR
    description = (
        "an items.parquet rollup must be registered as a geoparquet-items asset"
        " and a rel:'items' link"
    )
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        assets = rollup_assets(node)
        links = _items_links(node)
        if not assets and not links:
            return

        if not assets:
            yield self.finding(
                node,
                "rel:'items' link declares a rollup that no collection-level asset registers",
                json_pointer="/links",
                fix_hint=f"add the rollup as the '{ROLLUP_ASSET_KEY}' asset with roles"
                f' ["{ROLLUP_ROLE}"] and type {PARQUET_MEDIA_TYPE}',
            )
        if not links:
            yield self.finding(
                node,
                "rollup asset is not registered through a rel:'items' link",
                json_pointer="/links",
                fix_hint=f"add a link with rel:'items', the rollup href, and type"
                f" {PARQUET_MEDIA_TYPE}",
            )

        for pointer, key, asset in assets:
            if key != ROLLUP_ASSET_KEY:
                yield self.finding(
                    node,
                    f"rollup asset is keyed '{key}', expected '{ROLLUP_ASSET_KEY}'",
                    json_pointer=pointer,
                )
            if ROLLUP_ROLE not in roles_of(asset):
                yield self.finding(
                    node,
                    f"rollup asset '{key}' does not carry the '{ROLLUP_ROLE}' role",
                    json_pointer=f"{pointer}/roles",
                )
            if asset.get("type") != PARQUET_MEDIA_TYPE:
                yield self.finding(
                    node,
                    f"rollup asset '{key}' has type {asset.get('type')!r},"
                    f" expected '{PARQUET_MEDIA_TYPE}'",
                    json_pointer=f"{pointer}/type",
                )

        for index, link in links:
            if link.get("type") != PARQUET_MEDIA_TYPE:
                yield self.finding(
                    node,
                    f"rel:'items' link has type {link.get('type')!r},"
                    f" expected '{PARQUET_MEDIA_TYPE}'",
                    json_pointer=f"/links/{index}/type",
                )
