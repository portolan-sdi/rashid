"""Structural link rules (spec: core.md, Links)."""

from __future__ import annotations

from collections.abc import Iterable

from rashid.catalog import CatalogGraph, Node, is_absolute_href
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import STRUCTURAL_RELS, links_of


class RequiredLinksRule(Rule):
    """Every object carries its required structural links."""

    id = "PTL-LNK-001"
    spec_ids = ("PORTO-CORE-019", "PORTO-CORE-032")
    default_severity = Severity.ERROR
    description = "catalogs/collections need root+parent links; items need root, parent, collection"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        rels = {link.get("rel") for link in links_of(node)}
        required = ["root", "parent", "collection"] if node.kind == "item" else ["root", "parent"]
        if node is graph.root:
            required.remove("parent")
        for rel in required:
            if rel not in rels:
                yield self.finding(
                    node,
                    f"missing required structural link rel:'{rel}'",
                    json_pointer="/links",
                )


class ChildLinkCompletenessRule(Rule):
    """A child or item link exists for every object the node contains."""

    id = "PTL-LNK-002"
    spec_ids = ("PORTO-CORE-032",)
    default_severity = Severity.ERROR
    description = "every contained object is reachable through a child or item link"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        linked: set[object] = set()
        for link in links_of(node):
            if link.get("rel") not in ("child", "item"):
                continue
            href = link.get("href")
            if isinstance(href, str):
                target = graph.resolve_link(node, href)
                if target is not None:
                    linked.add(target.path)
        for contained in graph.children_of(node):
            if contained.path in linked:
                continue
            expected = "item" if contained.kind == "item" else "child"
            yield self.finding(
                node,
                f"contained object '{contained.path}' has no {expected} link",
                json_pointer="/links",
                fix_hint=f"add a rel:'{expected}' link pointing to {contained.path}",
            )


class StructuralLinkTypeRule(Rule):
    """Structural links declare the correct media type."""

    id = "PTL-LNK-003"
    spec_ids = ("PORTO-CORE-033",)
    default_severity = Severity.ERROR
    description = "structural links carry application/json (application/geo+json for item links)"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in enumerate(links_of(node)):
            rel = link.get("rel")
            if rel not in STRUCTURAL_RELS:
                continue
            expected = "application/geo+json" if rel == "item" else "application/json"
            actual = link.get("type")
            if actual != expected:
                yield self.finding(
                    node,
                    f"link rel:'{rel}' has type {actual!r}, expected '{expected}'",
                    json_pointer=f"/links/{index}/type",
                )


class RelativeLinksRule(Rule):
    """Structural links are relative."""

    id = "PTL-LNK-004"
    spec_ids = ("PORTO-CORE-034",)
    default_severity = Severity.ERROR
    description = "structural links must be relative for catalog portability"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in enumerate(links_of(node)):
            rel = link.get("rel")
            if rel not in STRUCTURAL_RELS:
                continue
            href = link.get("href")
            if not isinstance(href, str) or not href:
                yield self.finding(
                    node,
                    f"link rel:'{rel}' has no href",
                    json_pointer=f"/links/{index}/href",
                )
            elif is_absolute_href(href):
                yield self.finding(
                    node,
                    f"structural link rel:'{rel}' must be relative, got '{href}'",
                    json_pointer=f"/links/{index}/href",
                )


class NoSelfLinkRule(Rule):
    """Objects carry no self link."""

    id = "PTL-LNK-005"
    spec_ids = ("PORTO-CORE-034",)
    default_severity = Severity.ERROR
    description = "objects must not include a self link (pystac SELF_CONTAINED convention)"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in enumerate(links_of(node)):
            if link.get("rel") == "self":
                yield self.finding(
                    node,
                    "object includes a rel:'self' link",
                    json_pointer=f"/links/{index}",
                    fix_hint="remove the self link; Portolan catalogs are self-contained",
                )


class LinkResolutionRule(Rule):
    """Every relative structural link resolves to the correct object."""

    id = "PTL-LNK-006"
    spec_ids = ("PORTO-CORE-015", "PORTO-CORE-035", "PORTO-CORE-036")
    default_severity = Severity.ERROR
    description = "structural links must resolve against the file tree to the correct object"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        parent = graph.parent_of(node)
        for index, link in enumerate(links_of(node)):
            rel = link.get("rel")
            if rel not in STRUCTURAL_RELS:
                continue
            href = link.get("href")
            if not isinstance(href, str) or not href or is_absolute_href(href):
                continue  # PTL-LNK-004 reports these
            pointer = f"/links/{index}/href"
            target = graph.resolve_link(node, href)
            if target is None:
                resolved = graph.resolve_path(node, href)
                if resolved is not None and graph.file_exists(resolved):
                    message = (
                        f"link rel:'{rel}' href '{href}' resolves to a file"
                        " that is not a recognizable STAC object"
                    )
                else:
                    message = f"link rel:'{rel}' href '{href}' does not resolve to any file"
                yield self.finding(node, message, json_pointer=pointer)
                continue
            if target.parse_error is not None:
                yield self.finding(
                    node,
                    f"link rel:'{rel}' href '{href}' resolves to an unparseable file",
                    json_pointer=pointer,
                )
                continue
            wrong: str | None = None
            if rel == "root" and target is not graph.root:
                wrong = "must point to the root catalog"
            elif rel == "parent" and target is not parent:
                wrong = "must point to the containing object" + (
                    f" ({parent.path})" if parent is not None else ""
                )
            elif rel == "collection" and (target.kind != "collection" or target is not parent):
                wrong = "must point to the item's enclosing collection"
            elif rel == "child" and (
                target.kind not in ("catalog", "collection") or graph.parent_of(target) is not node
            ):
                wrong = "must point to a catalog or collection contained by this object"
            elif rel == "item" and (target.kind != "item" or graph.parent_of(target) is not node):
                wrong = "must point to an item contained by this object"
            if wrong is not None:
                yield self.finding(
                    node,
                    f"link rel:'{rel}' href '{href}' points to the wrong object: {wrong}",
                    json_pointer=pointer,
                )
