"""Collection-modeling rules (spec: core.md, Core Structure / Collections /
Single-File Collections).

A collection holding one data file MUST expose that file as a collection-level
asset, with no item directory and no item JSON (core.md:139-141). The tabular
section restates the same requirement for Parquet (formats.md:163-166). The
check is structural: one item wrapping the collection's only data asset is the
shape the spec rules out.

Collections are also leaves: "collections MUST be one level deep, containing
only items or assets, never nested collections" (core.md, Core Structure), and
their IDs follow naming conventions (core.md, Collections).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from reis.catalog import CatalogGraph, Node
from reis.model import Finding, Severity
from reis.rule import Rule
from reis.rules._common import roles_of
from reis.rules.assets import _assets_of

_HAS_DATA_ASSET = "has-data-asset"
_NO_DATA_ASSET = "no-data-asset"
_UNKNOWN = "unknown"

# core.md, Collections: "Collection IDs SHOULD contain only lowercase letters,
# numbers, hyphens, and underscores, start with a letter". A nested collection's
# ID is a POSIX path (e.g. environment/air-quality), so each path segment is
# held to the convention.
_ID_SEGMENT = re.compile(r"^[a-z][a-z0-9_-]*$")


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
                )
                return


def _is_partitioned(node: Node) -> bool:
    return any(key.startswith("partition:") for key in node.data)


def _data_asset_state(node: Node) -> str:
    """Whether the collection exposes its data itself, and whether that is knowable."""
    for _pointer, _key, asset in _assets_of(node):
        roles = roles_of(asset)
        if not roles:
            return _UNKNOWN
        if "data" in roles:
            return _HAS_DATA_ASSET
    return _NO_DATA_ASSET
