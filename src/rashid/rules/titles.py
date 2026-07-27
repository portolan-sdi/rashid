"""Human-readable title rules (spec: core.md, Human-Readable Titles)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import links_of

# A "raw slug": word characters chained with _ or . and no spaces at all,
# e.g. road_centerlines_2024. Single words without separators are allowed.
_SLUG = re.compile(r"^\w+([_.]\w+)+$")
# A technical namespace prefix, e.g. ns:LayerName.
_NAMESPACE = re.compile(r"^[A-Za-z]\w*:\S")


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
                yield self.finding(node, f"missing or empty '{field}'", json_pointer=f"/{field}")


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
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        title = node.data.get("title")
        if not isinstance(title, str) or not title.strip():
            return  # PTL-TTL-001 reports the absence
        stripped = title.strip()
        if _SLUG.match(stripped):
            yield self.finding(
                node,
                f"title '{stripped}' looks like a raw slug, not a human-readable title",
                json_pointer="/title",
                fix_hint="use natural language, e.g. 'Road Centerlines 2024'",
            )
        elif _NAMESPACE.match(stripped):
            yield self.finding(
                node,
                f"title '{stripped}' carries a technical namespace prefix",
                json_pointer="/title",
                fix_hint="drop the namespace and use natural language",
            )


class LinkTitleRule(Rule):
    """Every child and item link carries a title."""

    id = "PTL-TTL-003"
    spec_ids = ("PORTO-CORE-039",)
    default_severity = Severity.ERROR
    description = "every child and item link must include a title"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in enumerate(links_of(node)):
            rel = link.get("rel")
            if rel not in ("child", "item"):
                continue
            title = link.get("title")
            if not isinstance(title, str) or not title.strip():
                yield self.finding(
                    node,
                    f"link rel:'{rel}' href '{link.get('href')}' has no title",
                    json_pointer=f"/links/{index}/title",
                )
