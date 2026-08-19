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
from rashid.rules._common import TIFF_SUFFIXES, is_raster_data_asset, roles_of
from rashid.rules.assets import _assets_of
from rashid.rules.item_mirror import mirror_assets

_HAS_DATA_ASSET = "has-data-asset"
_NO_DATA_ASSET = "no-data-asset"
_UNKNOWN = "unknown"

# core.md, Collections: "Collection IDs SHOULD contain only lowercase letters,
# numbers, hyphens, and underscores, start with a letter". A nested collection's
# ID is a POSIX path (e.g. environment/air-quality), so each path segment is
# held to the convention.
_ID_SEGMENT = re.compile(r"^[a-z][a-z0-9_-]*$")

# PORTO-CORE-071 binds a collection "holding multiple raster scenes". One
# stray file is a leftover, not a scene-modeling violation, and a lone COG is
# a collection-level asset by PORTO-CORE-072.
_MIN_SCENES = 2

# Scene counting uses raster intent rather than an already-correct COG type.
# Portolan requires primary raster data to be COG, but a bad declaration must
# not hide the scene-modeling defect. PTL-AST-006 owns the media-type finding.


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
        keys = _raster_asset_keys(node)
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
            if item.kind == "item" and _raster_asset_keys(item)
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


class MissingItemTreeRule(Rule):
    """A collection whose scenes or mirror imply items must publish them.

    core.md, Raster Collections: "A collection holding multiple raster scenes
    MUST model each scene as an item carrying its COG as an item-level asset"
    (``PORTO-CORE-071``). core.md, Core Structure: a collection directory
    "holds one subdirectory per item" (``PORTO-CORE-015``), and Links requires
    "a `child` or `item` link for every object it contains"
    (``PORTO-CORE-032``). formats.md, Raster: "The item JSON remains the
    normative representation", so an ``items.parquet`` mirror is a derived
    copy that "MUST exactly reproduce the collection's items"
    (``PORTO-FMT-042``), never a replacement for them.

    Two independent signals say a collection owes items it never published:

    - scene files sit in the collection directory that no asset declares. The
      directory listing is the only witness, since nothing in the metadata
      mentions them;
    - the collection registers an item mirror. That registration is itself a
      claim that items exist, and it stands whether or not the Parquet bytes
      can be read.

    The disk signal wins when both hold, so one collection yields one finding
    and the more specific repair is the one reported.

    Every other rule in this area keys off something this shape lacks:
    ``PTL-COL-004`` reads declared collection-level assets, ``PTL-COL-001``
    needs exactly one item, ``PTL-MIR-001`` needs scene items, and
    ``PTL-LNK-002`` can only miss a link to an object that exists. That last
    one is the hand-off: the moment a publisher writes item JSON without
    links, ``PTL-LNK-002`` takes over and this rule stands down.

    Alternate-language trees need no carve-out. The listing is read per
    directory and containment resolves within one tree, so each translation is
    judged against its own directory and its own items.
    """

    id = "PTL-COL-005"
    spec_ids = ("PORTO-CORE-015", "PORTO-CORE-032", "PORTO-CORE-071", "PORTO-FMT-042")
    default_severity = Severity.ERROR
    description = "a collection whose scene files or item mirror imply items must publish them"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        # A partitioned collection MAY leave its parts off items
        # (PORTO-CORE-021), so partition files are not scenes.
        if _is_partitioned(node):
            return
        # items_of, not children_of: the spec lets a catalog below a
        # collection organize its items, and those items still count.
        if graph.items_of(node):
            return
        scenes = _undeclared_scene_files(node, graph)
        if len(scenes) >= _MIN_SCENES:
            yield self.finding(
                node,
                f"collection directory holds {len(scenes)} raster scene file(s)"
                f" ({_name_some(scenes)}) but the collection publishes no items:"
                " no item JSON, no item directory, and no rel:'item' link"
                " (PORTO-CORE-015, PORTO-CORE-032, PORTO-CORE-071)",
                json_pointer="/links",
                fix_hint="create one item per scene, each in its own subdirectory,"
                " carrying that scene's COG as an item-level asset with its own"
                " geometry and datetime, and add a rel:'item' link to each",
            )
            return
        for pointer, key, _asset in mirror_assets(node):
            yield self.finding(
                node,
                f"collection registers item mirror '{key}' but publishes no items;"
                " the mirror is a derived copy and the item JSON remains the"
                " normative representation (PORTO-CORE-015, PORTO-CORE-032,"
                " PORTO-FMT-042)",
                json_pointer=pointer,
                fix_hint="write one item JSON per mirror row into its own subdirectory,"
                " add a rel:'item' link to each, and regenerate the mirror from them",
            )
            return


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
        ancestor = graph.enclosing_collection_of(node)
        if ancestor is not None:
            yield self.finding(
                node,
                f"collection is nested inside collection"
                f" '{ancestor.id or ancestor.path!s}'; collections must be one level deep",
                fix_hint="restructure so intermediate levels are catalogs"
                " and collections are leaves",
            )


class CollectionIdRule(Rule):
    """Collection IDs follow the naming convention and are unique.

    core.md, Collections: "Collection IDs SHOULD contain only lowercase
    letters, numbers, hyphens, and underscores, start with a letter, and be
    unique within the catalog." A nested collection's ID is a POSIX path from
    the catalog root (core.md, Nested Catalogs, Flat Collections), so the
    convention applies per path segment. SHOULD-level, so a WARNING.

    Uniqueness is judged inside one language tree. core.md,
    Alternate-Language Trees: an object repeated across trees keeps one ID,
    because that ID is what lets a client match the translations of one
    collection to each other, and a validator does not report the repeat.
    """

    id = "PTL-COL-003"
    spec_ids = ("PORTO-CORE-016", "PORTO-CORE-080")
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
        here = graph.language_root_of(node)
        for other in graph.iter("collection"):
            if other is node or other.id != node.id or other.path >= node.path:
                continue
            if graph.language_root_of(other) is here:
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


def _name_some(names: list[str], limit: int = 3) -> str:
    """The first few names, with a count standing in for the rest."""
    shown = ", ".join(names[:limit])
    remaining = len(names) - limit
    return f"{shown}, and {remaining} more" if remaining > 0 else shown


def _declared_filenames(node: Node) -> set[str]:
    """Basenames of every href the collection's own assets declare.

    Role is deliberately ignored. The defect this serves is an undeclared
    scene, so any declaration exempts a file: a retained upstream GeoTIFF
    (PORTO-FMT-035) and an alternate representation (PORTO-CORE-031) are both
    legitimate, and a wrong role is PTL-AST-001's finding, not this one.
    """
    declared: set[str] = set()
    for _pointer, _key, asset in _assets_of(node):
        href = asset.get("href")
        if isinstance(href, str) and href:
            declared.add(href.rsplit("/", 1)[-1])
    return declared


def _undeclared_scene_files(node: Node, graph: CatalogGraph) -> list[str]:
    """Raster files beside the collection that no asset of it declares."""
    listing = graph.dir_listing.get(node.path.parent, set())
    declared = _declared_filenames(node)
    return sorted(
        name for name in listing if name.lower().endswith(TIFF_SUFFIXES) and name not in declared
    )


def _is_partitioned(node: Node) -> bool:
    return any(key.startswith("partition:") for key in node.data)


def _raster_asset_keys(node: Node) -> list[str]:
    """Keys of the node's primary raster assets, in declaration order."""
    keys: list[str] = []
    for _pointer, key, asset in _assets_of(node):
        if is_raster_data_asset(asset):
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
