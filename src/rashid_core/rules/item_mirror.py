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

from rashid_core._media import PARQUET_MEDIA_TYPE, is_raster_data_asset
from rashid_core._stac import MIRROR_ROLE, assets_of, mirror_assets, roles_of
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.model import Finding, Severity, json_pointer
from rashid_core.rule import Rule


def _scene_items(node: Node, graph: CatalogGraph) -> list[Node]:
    return [
        item
        for item in graph.items_of(node)
        if any(is_raster_data_asset(asset) for _pointer, _key, asset in assets_of(item))
    ]


class ItemMirrorPresentRule(Rule):
    """A raster collection with scene items publishes an items.parquet mirror.

    Scoped to the shape the spec names: items that carry raster data, which
    Portolan requires to be COGs. Raster intent is detected independently of
    a correct COG media type so ``PTL-AST-006`` and this rule can both report
    in one run. A collection
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
            json_pointer=json_pointer("assets"),
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
                    json_pointer=json_pointer("roles", prefix=pointer),
                    fix_hint=f'add "{MIRROR_ROLE}" to the asset\'s roles',
                )
            if asset.get("type") != PARQUET_MEDIA_TYPE:
                yield self.finding(
                    node,
                    f"mirror asset '{key}' has type {asset.get('type')!r},"
                    f" expected '{PARQUET_MEDIA_TYPE}'",
                    json_pointer=json_pointer("type", prefix=pointer),
                    fix_hint=f"set the mirror asset's type to '{PARQUET_MEDIA_TYPE}'",
                    expected=PARQUET_MEDIA_TYPE,
                    actual=asset.get("type"),
                )
