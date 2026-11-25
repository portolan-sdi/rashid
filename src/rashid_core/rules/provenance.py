"""Source-provenance rules (spec: core.md, Source Provenance).

Official vs mirror is derived from a collection's providers: official when
the producer and host are the same organization, mirror when they differ.
When providers are malformed these rules stay silent — the provider rules
already report that, and provenance cannot be derived from broken input.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from rashid_core._media import media_type_essence
from rashid_core._stac import links_of, parse_rfc3339, providers_of, roles_of
from rashid_core.catalog import CatalogGraph, Node
from rashid_core.model import Finding, Severity, json_pointer
from rashid_core.rule import Rule

Provenance = Literal["official", "mirror"]


def provenance_of(node: Node) -> Provenance | None:
    """Derive a collection's provenance, or None when underivable."""
    providers = providers_of(node)
    producers = [p for p in providers if "producer" in roles_of(p)]
    hosts = [p for p in providers if "host" in roles_of(p)]
    if not producers or len(hosts) != 1:
        return None
    host_name = _normalized_name(hosts[0])
    if host_name is None:
        return None
    for producer in producers:
        if _normalized_name(producer) == host_name:
            return "official"
    return "mirror"


def _normalized_name(provider: dict[str, object]) -> str | None:
    name = provider.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().casefold()


class MirrorViaLinkRule(Rule):
    """A mirror links back to its original source."""

    id = "PTL-PRO-001"
    spec_ids = ("PORTO-CORE-053", "PORTO-CORE-084")
    default_severity = Severity.ERROR
    description = "a mirror must include at least one HTML via link to the original source"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if provenance_of(node) != "mirror":
            return
        if any(
            link.get("rel") == "via" and media_type_essence(link.get("type")) == "text/html"
            for link in links_of(node)
        ):
            return
        yield self.finding(
            node,
            "mirror collection has no rel:'via' link of type 'text/html' to its original source",
            json_pointer=json_pointer("links"),
            fix_hint='add a via link to the upstream landing page with type "text/html";'
            " keep service links with the media types their services return",
        )


class MirrorCanonicalLinkRule(Rule):
    """A mirror of a STAC-publishing upstream links to the upstream STAC.

    Whether the upstream publishes STAC is unknowable from metadata alone,
    so a missing canonical link is INFO, not a warning.
    """

    id = "PTL-PRO-002"
    spec_ids = ("PORTO-CORE-054", "PORTO-CORE-055")
    default_severity = Severity.INFO
    description = "a mirror should link the upstream STAC with rel:'canonical' when one exists"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if provenance_of(node) != "mirror":
            return
        if not any(link.get("rel") == "canonical" for link in links_of(node)):
            yield self.finding(
                node,
                "mirror collection has no rel:'canonical' link; add one if the upstream"
                " publishes its own STAC catalog",
                json_pointer=json_pointer("links"),
                fix_hint='add {"rel": "canonical", "href": <the upstream collection.json>,'
                ' "type": "application/json"} to links when the upstream publishes STAC',
            )


class MirrorUpdatedRule(Rule):
    """A mirror records its last sync time in the top-level updated field.

    Applies to every mirror collection, and to every catalog, the root or a
    sub-catalog, whose collections are all mirrors: a mirror sync sets
    ``updated`` "on every catalog and collection it checks" (PORTO-CORE-057).
    """

    id = "PTL-PRO-003"
    spec_ids = ("PORTO-CORE-057",)
    default_severity = Severity.ERROR
    description = "a mirror must record sync time in a top-level RFC 3339 'updated' field"
    kinds = ()  # graph-level: the root-catalog part needs the whole tree

    def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
        provenances = {node.path: provenance_of(node) for node in graph.iter("collection")}
        for node in graph.iter("collection"):
            if provenances[node.path] == "mirror":
                yield from self._check_updated(node)
        for catalog in graph.iter("catalog"):
            under = _collections_under(graph, catalog)
            if under and all(provenances.get(c.path) == "mirror" for c in under):
                yield from self._check_updated(catalog)

    def _check_updated(self, node: Node) -> Iterable[Finding]:
        value = node.data.get("updated")
        if value is None:
            yield Finding(
                rule_id=self.id,
                severity=self.default_severity,
                message="mirror has no top-level 'updated' field recording the last sync",
                path=str(node.path),
                object_id=node.id,
                json_pointer=json_pointer("updated"),
                fix_hint="add a top-level 'updated' field holding the last sync time as an"
                " RFC 3339 date-time, e.g. '2024-01-01T00:00:00Z'",
            )
        elif parse_rfc3339(value) is None:
            yield Finding(
                rule_id=self.id,
                severity=self.default_severity,
                message=f"'updated' value {value!r} is not an RFC 3339 date-time",
                path=str(node.path),
                object_id=node.id,
                json_pointer=json_pointer("updated"),
                fix_hint="rewrite 'updated' as an RFC 3339 date-time, e.g. '2024-01-01T00:00:00Z'",
                actual=value,
            )


def _collections_under(graph: CatalogGraph, catalog: Node) -> list[Node]:
    """Every collection below ``catalog``, through sub-catalogs and nested collections.

    Containment parents sit in shallower directories, so the walk is a tree.
    """
    found: list[Node] = []
    pending = [catalog]
    while pending:
        for child in graph.children_of(pending.pop()):
            if child.kind == "collection":
                found.append(child)
            pending.append(child)
    return found


class OfficialNoUpstreamLinksRule(Rule):
    """An official catalog carries no via/canonical links to an upstream.

    A WARNING: the sentence in core.md, Source Provenance, carries no RFC 2119
    keyword, so the manifest gives it no ID and it is advice, not a MUST.
    """

    id = "PTL-PRO-004"
    default_severity = Severity.WARNING
    description = "an official collection should not carry via or canonical upstream links"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if provenance_of(node) != "official":
            return
        for rel in ("via", "canonical"):
            if any(link.get("rel") == rel for link in links_of(node)):
                yield self.finding(
                    node,
                    f"official collection carries a rel:'{rel}' link; it is the source,"
                    " not a mirror",
                    json_pointer=json_pointer("links"),
                    fix_hint=f"remove the rel:'{rel}' link, or, if this collection really"
                    " mirrors someone else's data, name the upstream as the producer in"
                    " providers",
                )
