"""Human-readable title rules (spec: core.md, Human-Readable Titles)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity, json_pointer
from rashid.rule import Rule

# A "raw slug": word characters chained with _ or . and no spaces at all,
# e.g. road_centerlines_2024. Single words without separators are allowed.
_SLUG = re.compile(r"^\w+([_.]\w+)+$")
# A technical namespace prefix, e.g. ns:LayerName.
_NAMESPACE = re.compile(r"^[A-Za-z]\w*:\S")

# What each required field should say, for the fix hints.
_FIELD_HINTS = {
    "title": "a human-readable name, e.g. 'Road Centerlines 2024'",
    "description": "a sentence or two saying what the data is and who it is for",
}


class TitleDescriptionRule(Rule):
    """Every catalog and collection has a non-empty title and description."""

    id = "PTL-TTL-001"
    spec_ids = ("PORTO-CORE-037",)
    default_severity = Severity.ERROR
    description = "catalog.json and collection.json need a non-empty title and description"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for field in ("title", "description"):
            value = node.data.get(field)
            if not isinstance(value, str) or not value.strip():
                yield self.finding(
                    node,
                    f"missing or empty '{field}'",
                    json_pointer=json_pointer(field),
                    fix_hint=f"add a '{field}': {_FIELD_HINTS[field]}",
                )


class HumanReadableTitleRule(Rule):
    """Titles look human-readable, not like slugs or namespaced layer names.

    The spec makes human-readable titles a MUST, but readability is checked
    heuristically and heuristics misfire, so this defaults to WARNING; use a
    severity override to promote it to ERROR.
    """

    id = "PTL-TTL-002"
    spec_ids = ("PORTO-CORE-038", "PORTO-CORE-040")
    default_severity = Severity.WARNING
    description = "titles must be human-readable, not raw slugs or ns:LayerName identifiers"
    kinds = ()  # One pass over all title positions, including optional Item titles.

    def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
        for node in graph.iter("catalog", "collection", "item"):
            if node.parse_error is not None:
                continue
            for pointer, title in _title_positions(node):
                if not isinstance(title, str) or not title.strip():
                    continue  # PTL-TTL-001/003 own required-title presence.
                stripped = title.strip()
                if _SLUG.match(stripped):
                    yield self.finding(
                        node,
                        f"title '{stripped}' looks like a raw slug, not a human-readable title",
                        json_pointer=pointer,
                        fix_hint="use natural language, e.g. 'Road Centerlines 2024'",
                    )
                elif _NAMESPACE.match(stripped):
                    yield self.finding(
                        node,
                        f"title '{stripped}' carries a technical namespace prefix",
                        json_pointer=pointer,
                        fix_hint="drop the namespace and use natural language",
                    )


def _title_positions(node: Node) -> Iterable[tuple[str, Any]]:
    if node.kind == "item":
        properties = node.data.get("properties")
        if isinstance(properties, dict):
            yield json_pointer("properties", "title"), properties.get("title")
    else:
        yield json_pointer("title"), node.data.get("title")
        for index, link in _child_item_links(node):
            yield json_pointer("links", index, "title"), link.get("title")


class LinkTitleRule(Rule):
    """Every child and item link carries a title."""

    id = "PTL-TTL-003"
    spec_ids = ("PORTO-CORE-039",)
    default_severity = Severity.ERROR
    description = "every child and item link must include a title"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in _child_item_links(node):
            rel = link.get("rel")
            title = link.get("title")
            if not isinstance(title, str) or not title.strip():
                yield self.finding(
                    node,
                    f"link rel:'{rel}' href '{link.get('href')}' has no title",
                    json_pointer=json_pointer("links", index, "title"),
                    fix_hint="add a title to the link, matching the title of the object it"
                    " points at",
                )


def _child_item_links(node: Node) -> Iterable[tuple[int, dict[str, Any]]]:
    links = node.data.get("links")
    if not isinstance(links, list):
        return
    for index, link in enumerate(links):
        if isinstance(link, dict) and link.get("rel") in ("child", "item"):
            yield index, link
