"""Collection-modeling rules (spec: core.md, Core Structure / Collections /
Single-File Collections / Raster Collections).

A collection holding one data file MUST expose that file as a collection-level
asset, with no item directory and no item JSON (core.md:139-141). The tabular
section restates the same requirement for Parquet (formats.md:163-166). The
check is structural: one item wrapping the collection's only data asset is the
shape the spec rules out.

Raster Collections turns the same structural question the other way round: a
collection of several scenes MUST carry each scene's COG on an item rather than
on the collection.

Collections are also leaves: "collections MUST be one level deep, containing
only items or assets, never nested collections" (core.md, Core Structure), and
their IDs follow naming conventions (core.md, Collections).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import is_cog_media_type, roles_of
from rashid.rules.assets import _assets_of

_HAS_DATA_ASSET = "has-data-asset"
_NO_DATA_ASSET = "no-data-asset"
_UNKNOWN = "unknown"

# core.md, Collections: "Collection IDs SHOULD contain only lowercase letters,
# numbers, hyphens, and underscores, start with a letter". A nested collection's
# ID is a POSIX path (e.g. environment/air-quality), so each path segment is
# held to the convention.
_ID_SEGMENT = re.compile(r"^[a-z][a-z0-9_-]*$")

# Scene counting matches on the COG profile through is_cog_media_type,
# following core.md: a scene is an item carrying its COG. A raster asset
# typed without the profile is not a scene, and the defect in its media type
# belongs to PTL-DAT-004 and PTL-FMT rather than to a modelling rule.


class SingleFileCollectionRule(Rule):
    """A collection's only data file belongs at collection level, not in an item.

    Fires on the narrow shape the spec names: one item, one data asset on it,
    and nothing carrying the ``data`` role on the collection itself. Anything
    wider — several items, several data assets, a collection that already
    exposes its data — is a legitimate multi-file collection and is left alone,
    so multi-scene raster collections stay clean whichever way
    portolan-sdi/portolan-spec#73 settles item-level raster modeling.

    Partitioned collections are exempt: they MAY represent each partition as an
    item (core.md:187-189), so a one-partition collection is a modeling choice
    the spec permits rather than a violation. A collection whose own assets are
    missing ``roles`` is also skipped, since roles are what identify the data
    asset and ``PTL-AST-001`` already reports the gap.
    """

    id = "PTL-COL-001"
    spec_ids = ("PORTO-CORE-017", "PORTO-FMT-034")
    default_severity = Severity.ERROR
    description = "a single-file collection must expose its data as a collection-level asset"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if _is_partitioned(node):
            return
        if _data_asset_state(node) is not _NO_DATA_ASSET:
            return
        items = [child for child in graph.children_of(node) if child.kind == "item"]
        if len(items) != 1:
            return
        item = items[0]
        data_assets = [
            (key, asset) for _p, key, asset in _assets_of(item) if "data" in roles_of(asset)
        ]
        if len(data_assets) != 1:
            return
        key, _asset = data_assets[0]
        yield self.finding(
            node,
            f"collection holds a single data file, but item {item.id or item.path!s} wraps it"
            f" as asset '{key}'; single-file collections carry their data at collection level",
            json_pointer="/assets",
            fix_hint=f"move the item's '{key}' asset into the collection's assets, then delete"
            f" the item JSON, its directory, and the collection's rel:'item' link to it",
        )


class RasterSceneItemRule(Rule):
    """Scene COGs belong on items; only a lone COG sits at collection level.

    core.md, Raster Collections: "A collection holding multiple raster scenes
    MUST model each scene as an item carrying its COG as an item-level asset;
    scene COGs MUST NOT be listed as collection-level assets"
    (``PORTO-CORE-071``), and "A collection holding a single COG follows the
    single-file rule above: it MUST expose that COG as a collection-level asset
    with no item directory" (``PORTO-CORE-072``).

    Two shapes are wrong, and both put a scene COG on the collection:

    - two or more COG data assets on the collection — a multi-scene collection
      flattened into one asset list, where per-scene footprints and acquisition
      times have nowhere to live;
    - one COG on the collection alongside items that carry COGs of their own —
      a multi-scene collection with one scene left behind at collection level,
      which is also the item directory ``PORTO-CORE-072`` rules out for the
      single-COG case.

    The complementary shape — a lone COG wrapped in the collection's only item,
    with nothing at collection level — is ``PTL-COL-001``'s, which fires for any
    data format; this rule stays off it so the two never double-report. A
    collection whose only COG sits at collection level with no raster items is
    exactly what ``PORTO-CORE-072`` prescribes, and a collection whose scenes
    all live on items carries no collection-level COG at all: both stay clean.
    """

    id = "PTL-COL-004"
    spec_ids = ("PORTO-CORE-071", "PORTO-CORE-072")
    default_severity = Severity.ERROR
    description = "raster scenes belong on items; only a single-COG collection carries one itself"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        keys = _cog_asset_keys(node)
        if len(keys) > 1:
            listed = ", ".join(f"'{key}'" for key in keys)
            yield self.finding(
                node,
                f"collection lists {len(keys)} scene COGs as collection-level assets"
                f" ({listed}); each raster scene belongs on its own item",
                json_pointer="/assets",
                fix_hint="create one item per scene, move each COG onto its item as an"
                " item-level asset with that scene's geometry and datetime, and link the"
                " items from the collection with rel:'item'",
            )
            return
        if not keys:
            return
        scene_items = [
            item
            for item in graph.children_of(node)
            if item.kind == "item" and _cog_asset_keys(item)
        ]
        if not scene_items:
            return
        names = ", ".join(str(item.id or item.path) for item in scene_items[:3])
        yield self.finding(
            node,
            f"collection exposes COG asset '{keys[0]}' at collection level while items"
            f" ({names}) carry scene COGs; a collection-level COG is only for a"
            " single-COG collection, which has no item directory",
            json_pointer=f"/assets/{keys[0]}",
            fix_hint=f"move the '{keys[0]}' asset onto an item of its own alongside the"
            " other scenes",
        )


class NestedCollectionRule(Rule):
    """Collections are leaves; a collection inside a collection is forbidden.

    core.md, Core Structure: "collections MUST be one level deep, containing
    only items or assets, never nested collections", restated under Nested
    Catalogs, Flat Collections as "A collection MUST NOT contain a child
    collection." A catalog below a collection is legitimate (the spec allows
    one to organize many items), so the check walks the containment chain
    looking for a collection ancestor, whatever sits in between.
    """

    id = "PTL-COL-002"
    spec_ids = ("PORTO-CORE-004", "PORTO-CORE-014", "PORTO-CORE-018")
    default_severity = Severity.ERROR
    description = "collections must never nest: no collection may contain another collection"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        ancestor = graph.parent_of(node)
        while ancestor is not None:
            if ancestor.kind == "collection":
                yield self.finding(
                    node,
                    f"collection is nested inside collection"
                    f" '{ancestor.id or ancestor.path!s}'; collections must be one level deep",
                    fix_hint="restructure so intermediate levels are catalogs"
                    " and collections are leaves",
                )
                return
            ancestor = graph.parent_of(ancestor)


class CollectionIdRule(Rule):
    """Collection IDs follow the naming convention and are unique.

    core.md, Collections: "Collection IDs SHOULD contain only lowercase
    letters, numbers, hyphens, and underscores, start with a letter, and be
    unique within the catalog." A nested collection's ID is a POSIX path from
    the catalog root (core.md, Nested Catalogs, Flat Collections), so the
    convention applies per path segment. SHOULD-level, so a WARNING.
    """

    id = "PTL-COL-003"
    spec_ids = ("PORTO-CORE-016",)
    default_severity = Severity.WARNING
    description = (
        "collection IDs should be lowercase [a-z0-9_-], start with a letter, and be unique"
    )
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if node.id is None:
            return  # the structural pass reports a missing id
        if not all(_ID_SEGMENT.match(segment) for segment in node.id.split("/")):
            yield self.finding(
                node,
                f"collection id '{node.id}' does not follow the naming convention"
                " (lowercase letters, numbers, hyphens, underscores; starts with a letter)",
                json_pointer="/id",
                fix_hint="rename the id to lowercase, replacing spaces and other characters"
                " with hyphens, e.g. 'road-centerlines-2024'",
                actual=node.id,
            )
        # Report a duplicate on every collection after the first, so the first
        # occurrence (in path order) stays clean and each clash is flagged once.
        for other in graph.iter("collection"):
            if other is not node and other.id == node.id and other.path < node.path:
                yield self.finding(
                    node,
                    f"collection id '{node.id}' is not unique within the catalog"
                    f" (also used by '{other.path}')",
                    json_pointer="/id",
                    fix_hint="give this collection a distinct id, e.g. prefix it with its"
                    " parent catalog's name",
                    actual=node.id,
                )
                return


def _is_partitioned(node: Node) -> bool:
    return any(key.startswith("partition:") for key in node.data)


def _cog_asset_keys(node: Node) -> list[str]:
    """Keys of the node's COG data assets, in declaration order."""
    keys: list[str] = []
    for _pointer, key, asset in _assets_of(node):
        if not is_cog_media_type(asset.get("type")):
            continue
        if "data" in roles_of(asset):
            keys.append(key)
    return keys


def _data_asset_state(node: Node) -> str:
    """Whether the collection exposes its data itself, and whether that is knowable."""
    for _pointer, _key, asset in _assets_of(node):
        roles = roles_of(asset)
        if not roles:
            return _UNKNOWN
        if "data" in roles:
            return _HAS_DATA_ASSET
    return _NO_DATA_ASSET
