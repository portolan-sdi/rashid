"""Structural link rules (spec: core.md, Links)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rashid.catalog import CatalogGraph, Node, is_absolute_href
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import STRUCTURAL_RELS, links_of

# What each required structural link points at, for the fix hints.
_REQUIRED_LINK_TARGETS = {
    "root": "the root catalog.json",
    "parent": "the JSON of the object that contains this one",
    "collection": "the item's enclosing collection.json",
}

# core.md, Catalog Logo: the media types a browser renders in an <img> element.
# A client drops an icon whose media type it does not recognize, so an icon
# outside this set renders nowhere even when the link itself is well formed.
ICON_MEDIA_TYPES = frozenset(
    {
        "image/apng",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/svg+xml",
        "image/webp",
    }
)


def _icon_links(node: Node) -> Iterable[tuple[int, dict[str, Any]]]:
    """Every rel:'icon' link on the node, with its index in the links array.

    core.md scopes publishing a logo to the root catalog, but says nothing to
    forbid one elsewhere, and STAC Browser renders a per-entity icon on
    collection and item cards. So the rules below constrain any icon link they
    find rather than only the root's.
    """
    for index, link in enumerate(links_of(node)):
        if link.get("rel") == "icon":
            yield index, link


class RequiredLinksRule(Rule):
    """Every object carries its required structural links.

    The root of an alternate-language tree is exempt from ``parent``, the same
    way the catalog's own root is. core.md, Alternate-Language Trees: each
    tree keeps its own root catalog with no ``parent`` link, and a validator
    does not report that as a failure.
    """

    id = "PTL-LNK-001"
    spec_ids = ("PORTO-CORE-019", "PORTO-CORE-032", "PORTO-CORE-080")
    default_severity = Severity.ERROR
    description = "catalogs/collections need root+parent links; items need root, parent, collection"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        rels = {link.get("rel") for link in links_of(node)}
        required = ["root", "parent", "collection"] if node.kind == "item" else ["root", "parent"]
        if node is graph.language_root_of(node):
            required.remove("parent")
        for rel in required:
            if rel not in rels:
                yield self.finding(
                    node,
                    f"missing required structural link rel:'{rel}'",
                    json_pointer="/links",
                    fix_hint=f"add a rel:'{rel}' link to links, with a relative href to"
                    f" {_REQUIRED_LINK_TARGETS[rel]} and type application/json",
                )


class ChildLinkCompletenessRule(Rule):
    """A child or item link exists for every object the node contains.

    An alternate-language tree sits in a subdirectory, so the walk that builds
    the graph reads its root catalog as an object the catalog contains. core.md,
    Alternate-Language Trees says otherwise: a translation stays outside the
    containment tree, and a validator does not report the missing ``child``
    link as a failure. :meth:`CatalogGraph.translation_roots` names the objects
    that carve-out covers.
    """

    id = "PTL-LNK-002"
    spec_ids = ("PORTO-CORE-032", "PORTO-CORE-080")
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
        translations = graph.translation_roots()
        for contained in graph.children_of(node):
            if contained.path in linked or contained in translations:
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
                    fix_hint=f"set the link's type to '{expected}'",
                    expected=expected,
                    actual=actual,
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
                    fix_hint="set the link's href to the target's path relative to this file,"
                    " e.g. '../catalog.json'",
                )
            elif is_absolute_href(href):
                yield self.finding(
                    node,
                    f"structural link rel:'{rel}' must be relative, got '{href}'",
                    json_pointer=f"/links/{index}/href",
                    fix_hint="replace the absolute URL with a path relative to this file,"
                    " e.g. '../catalog.json'",
                    actual=href,
                )


class LinkResolutionRule(Rule):
    """Every relative structural link resolves to the correct object.

    core.md, Links requires each link to resolve to "the correct object",
    which for an item's rel:'collection' link is the collection that encloses
    it. That is not always its direct parent. core.md, Core Structure allows
    "a catalog ... below a collection to organize its items", and in that
    layout an item's parent is the organizing catalog while its collection
    link points at the collection above. The check therefore compares the
    target against the nearest collection ancestor, not the direct parent.

    A ``root`` link is judged against the root of the language tree the object
    sits in. core.md, Alternate-Language Trees: each tree keeps its own root
    catalog, and that is what the tree's own ``root`` links point at, so a
    translated object pointing at the catalog's primary root would be the
    error here rather than the fix.
    """

    id = "PTL-LNK-006"
    spec_ids = ("PORTO-CORE-015", "PORTO-CORE-035", "PORTO-CORE-036", "PORTO-CORE-080")
    default_severity = Severity.ERROR
    description = "structural links must resolve against the file tree to the correct object"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        parent = graph.parent_of(node)
        enclosing = graph.enclosing_collection_of(node) if node.kind == "item" else None
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
                    hint = (
                        "point the href at the target's catalog.json, collection.json, or item JSON"
                    )
                else:
                    message = f"link rel:'{rel}' href '{href}' does not resolve to any file"
                    hint = (
                        "correct the href to the target's path relative to this file,"
                        " or restore the missing file"
                    )
                yield self.finding(node, message, json_pointer=pointer, fix_hint=hint, actual=href)
                continue
            if target.parse_error is not None:
                yield self.finding(
                    node,
                    f"link rel:'{rel}' href '{href}' resolves to an unparseable file",
                    json_pointer=pointer,
                    fix_hint=f"repair the JSON in {target.path} so it parses",
                    actual=href,
                )
                continue
            wrong: str | None = None
            if rel == "root" and target is not graph.language_root_of(node):
                wrong = "must point to the root catalog"
            elif rel == "parent" and target is not parent:
                wrong = "must point to the containing object" + (
                    f" ({parent.path})" if parent is not None else ""
                )
            elif rel == "collection" and (target.kind != "collection" or target is not enclosing):
                wrong = "must point to the item's enclosing collection" + (
                    f" ({enclosing.path})" if enclosing is not None else ""
                )
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
                    fix_hint=f"repoint the href: a rel:'{rel}' link {wrong}",
                    actual=href,
                )


class IconMediaTypeRule(Rule):
    """A logo link declares a media type a browser renders."""

    id = "PTL-LNK-007"
    spec_ids = ("PORTO-CORE-075",)
    default_severity = Severity.ERROR
    description = "a rel:'icon' link must declare a browser-displayable image media type"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        allowed = ", ".join(sorted(ICON_MEDIA_TYPES))
        for index, link in _icon_links(node):
            actual = link.get("type")
            if actual in ICON_MEDIA_TYPES:
                continue
            if actual is None:
                message = "logo link rel:'icon' declares no type"
                pointer = f"/links/{index}"
            else:
                message = f"logo link rel:'icon' has type {actual!r}, which no browser renders"
                pointer = f"/links/{index}/type"
            yield self.finding(
                node,
                message,
                json_pointer=pointer,
                fix_hint=f"set the link's type to one of {allowed}",
                actual=actual,
            )


class IconTitleRule(Rule):
    """A logo link carries the title a page needs as the image's alt text."""

    id = "PTL-LNK-008"
    spec_ids = ("PORTO-CORE-076",)
    default_severity = Severity.WARNING
    description = "a rel:'icon' link should carry a title for use as the accessible label"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in _icon_links(node):
            title = link.get("title")
            if isinstance(title, str) and title.strip():
                continue
            yield self.finding(
                node,
                "logo link rel:'icon' has no title",
                json_pointer=f"/links/{index}",
                fix_hint="add a title naming what the logo shows; a page that renders"
                " the logo uses it as the image's accessible label",
            )


class IconRelativeHrefRule(Rule):
    """A logo link points at an image the catalog carries."""

    id = "PTL-LNK-009"
    spec_ids = ("PORTO-CORE-077",)
    default_severity = Severity.WARNING
    description = "a rel:'icon' link should be relative so the catalog stays portable"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for index, link in _icon_links(node):
            href = link.get("href")
            if not isinstance(href, str) or not href:
                yield self.finding(
                    node,
                    "logo link rel:'icon' has no href",
                    json_pointer=f"/links/{index}/href",
                    fix_hint="set the href to the image's path relative to this file,"
                    " e.g. './_assets/logo.png'",
                )
            elif is_absolute_href(href):
                yield self.finding(
                    node,
                    f"logo link rel:'icon' should be relative, got '{href}'",
                    json_pointer=f"/links/{index}/href",
                    fix_hint="copy the image into the catalog and point the href at it,"
                    " e.g. './_assets/logo.png'",
                    actual=href,
                )


class ContainmentStaysInLanguageRule(Rule):
    """A child or item link does not reach into an alternate-language tree.

    core.md, Alternate-Language Trees: a translated tree stays outside the
    containment tree, so the catalog must not reach its objects through a
    ``child`` or ``item`` link. A publisher who links the translation as a
    child instead of an alternate gets two catalogs of the same data in a
    client's tree, one of them in a language the reader did not ask for.

    The constraint runs one way. A translated collection reaching back into
    the source tree for the items it did not translate is the shallow layout
    the best-practices page recommends, and is not reported here.
    """

    id = "PTL-LNK-010"
    spec_ids = ("PORTO-CORE-080",)
    default_severity = Severity.ERROR
    description = "child and item links must not reach into an alternate-language tree"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        translations = graph.translation_roots()
        if not translations:
            return
        here = graph.language_root_of(node)
        for index, link in enumerate(links_of(node)):
            rel = link.get("rel")
            if rel not in ("child", "item"):
                continue
            href = link.get("href")
            if not isinstance(href, str):
                continue
            target = graph.resolve_link(node, href)
            if target is None:
                continue  # PTL-LNK-005 reports a link that does not resolve
            there = graph.language_root_of(target)
            if there is here or there not in translations:
                continue
            language = there.data.get("language")
            code = language.get("code") if isinstance(language, dict) else None
            named = f" ({code})" if isinstance(code, str) else ""
            yield self.finding(
                node,
                f"link rel:'{rel}' reaches into the alternate-language tree at"
                f" '{there.path}'{named}",
                json_pointer=f"/links/{index}",
                fix_hint="change the link to rel:'alternate' with type 'application/json'"
                " and an hreflang naming the target's language",
                actual=href,
            )
