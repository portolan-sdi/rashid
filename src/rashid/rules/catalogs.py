"""Catalog-organization rules (spec: core.md, Nested Catalogs, Flat Collections).

Portolan puts catalogs at every level above a leaf, so a publisher can group
children thematically at any depth. core.md asks them to: "A catalog or
collection with twenty or more children SHOULD organize them into subcatalogs,
thematic or otherwise, so users can browse the data" (``PORTO-CORE-078``).
SHOULD-level, so a WARNING.

The requirement names both object types, because a collection has the same
problem: it links its items, and a flat list of hundreds reads no better than a
flat list of child collections. core.md permits the repair on both sides, since
a catalog may sit below a collection to organize its items.
"""

from __future__ import annotations

from collections.abc import Iterable

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule

# The spec's threshold, counted over ungrouped children only.
_FANOUT_THRESHOLD = 20


class SubcatalogFanoutRule(Rule):
    """A catalog or collection groups its children at twenty or more of them.

    The count covers the children the object has not grouped: collections and
    items directly beneath a catalog, items directly beneath a collection.
    Children that are themselves catalogs are the organization the requirement
    asks for, so they do not count toward the threshold. An object that already
    holds subcatalogs still reports when a flat tail of twenty or more ungrouped
    children remains beside them.
    """

    id = "PTL-CAT-001"
    spec_ids = ("PORTO-CORE-078",)
    default_severity = Severity.WARNING
    description = (
        "a catalog or collection with twenty or more ungrouped children should use subcatalogs"
    )
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        ungrouped = [child for child in graph.children_of(node) if child.kind != "catalog"]
        if len(ungrouped) < _FANOUT_THRESHOLD:
            return
        yield self.finding(
            node,
            f"{node.kind} holds {len(ungrouped)} children with no subcatalog grouping them;"
            " a flat list this long is hard to browse",
            fix_hint="group the children under thematic subcatalogs, each with its own"
            " catalog.json, and relink this object to those subcatalogs",
            actual=len(ungrouped),
            expected=f"fewer than {_FANOUT_THRESHOLD}",
        )
