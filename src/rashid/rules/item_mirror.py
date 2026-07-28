"""Item-mirror rules (spec: formats.md, Raster § Item mirror).

A raster collection that models scenes as items SHOULD publish a
stac-geoparquet mirror of those items at ``items.parquet``
(``PORTO-FMT-040``), and a published mirror MUST be registered as a
collection-level asset typed ``application/vnd.apache.parquet`` with the role
``collection-mirror`` (``PORTO-FMT-041``). That single registration is the
whole requirement: the stac-geoparquet spec asks for no companion link, and
neither does Portolan.

Both checks read metadata only. Whether the mirror's rows match the
collection's items is a bytes question, so it lives in the data pass
(``PTL-DAT-016``).
"""

from __future__ import annotations

from collections.abc import Iterable

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import is_cog_media_type, roles_of
from rashid.rules.assets import _assets_of

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
MIRROR_ROLE = "collection-mirror"
MIRROR_FILENAME = "items.parquet"


def mirror_assets(node: Node) -> list[tuple[str, str, dict[str, object]]]:
    """The node's item-mirror assets, found by role or href.

    Role is the normative signal, but a producer that gets the role wrong
    still means the asset as a mirror, and PTL-MIR-002 should say so rather
    than treat it as an unrelated Parquet file. The filename is the other way
    the same intent shows up. The asset key carries no signal: the
    stac-geoparquet spec names none, so the producer chooses it.
    """
    found = []
    for pointer, key, asset in _assets_of(node):
        href = asset.get("href")
        by_href = isinstance(href, str) and href.rsplit("/", 1)[-1] == MIRROR_FILENAME
        if MIRROR_ROLE in roles_of(asset) or by_href:
            found.append((pointer, key, asset))
    return found


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


def has_cog(node: Node) -> bool:
    """Whether the node carries at least one cloud-optimized GeoTIFF asset.

    A COG asset is what makes an item a scene. Any tool deciding whether a
    collection owes an items.parquet mirror needs the same answer rashid
    uses for ``PTL-MIR-001``.
    """
    return any(is_cog_media_type(asset.get("type")) for _pointer, _key, asset in _assets_of(node))


def _scene_items(node: Node, graph: CatalogGraph) -> list[Node]:
    return [child for child in graph.children_of(node) if child.kind == "item" and has_cog(child)]


class ItemMirrorPresentRule(Rule):
    """A raster collection with scene items publishes an items.parquet mirror.

    Scoped to the shape the spec names: items that carry COGs. A collection
    whose single COG sits at collection level has no items and owes no
    mirror, and a vector or point-cloud collection is outside the ratified
    SHOULD (mirroring those is still incubating). SHOULD-level, so WARNING.
    """

    id = "PTL-MIR-001"
    spec_ids = ("PORTO-FMT-040",)
    default_severity = Severity.WARNING
    description = "raster collections with scene items should publish an items.parquet mirror"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        scenes = _scene_items(node, graph)
        if not scenes:
            return
        if mirror_assets(node):
            return
        yield self.finding(
            node,
            f"raster collection models {len(scenes)} scene(s) as items but publishes no"
            " stac-geoparquet mirror",
            json_pointer="/assets",
            fix_hint="write the items to items.parquet in the collection root, then register it"
            f' as a collection-level asset with roles ["{MIRROR_ROLE}"] and type'
            f" {PARQUET_MEDIA_TYPE}",
        )


class ItemMirrorRegistrationRule(Rule):
    """A published mirror carries the role and media type the spec names.

    Fires only once a mirror exists in some form. A client finds the file by
    its role and reads it by its type, so getting either wrong hides the
    mirror from the readers it was written for.
    """

    id = "PTL-MIR-002"
    spec_ids = ("PORTO-FMT-041",)
    default_severity = Severity.ERROR
    description = (
        "an items.parquet mirror must be registered as a collection-level asset"
        " with the collection-mirror role"
    )
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, key, asset in mirror_assets(node):
            if MIRROR_ROLE not in roles_of(asset):
                yield self.finding(
                    node,
                    f"mirror asset '{key}' does not carry the '{MIRROR_ROLE}' role",
                    json_pointer=f"{pointer}/roles",
                    fix_hint=f'add "{MIRROR_ROLE}" to the asset\'s roles',
                )
            if asset.get("type") != PARQUET_MEDIA_TYPE:
                yield self.finding(
                    node,
                    f"mirror asset '{key}' has type {asset.get('type')!r},"
                    f" expected '{PARQUET_MEDIA_TYPE}'",
                    json_pointer=f"{pointer}/type",
                    fix_hint=f"set the mirror asset's type to '{PARQUET_MEDIA_TYPE}'",
                    expected=PARQUET_MEDIA_TYPE,
                    actual=asset.get("type"),
                )
