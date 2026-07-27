"""Source-provenance rules (spec: core.md, Source Provenance).

Official vs mirror is derived from a collection's providers: official when
the producer and host are the same organization, mirror when they differ.
When providers are malformed these rules stay silent — the provider rules
already report that, and provenance cannot be derived from broken input.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule
from rashid.rules._common import links_of, parse_rfc3339
from rashid.rules.providers import providers_of

Provenance = Literal["official", "mirror"]


def provenance_of(node: Node) -> Provenance | None:
    """Derive a collection's provenance, or None when underivable."""
    providers = providers_of(node)
    producers = [p for p in providers if "producer" in _roles(p)]
    hosts = [p for p in providers if "host" in _roles(p)]
    if not producers or len(hosts) != 1:
        return None
    host_name = _normalized_name(hosts[0])
    if host_name is None:
        return None
    for producer in producers:
        if _normalized_name(producer) == host_name:
            return "official"
    return "mirror"


def _roles(provider: dict[str, object]) -> list[str]:
    raw = provider.get("roles")
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str)]


def _normalized_name(provider: dict[str, object]) -> str | None:
    name = provider.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().casefold()


class MirrorViaLinkRule(Rule):
    """A mirror links back to its original source."""

    id = "PTL-PRO-001"
    spec_ids = ("PORTO-CORE-053",)
    default_severity = Severity.ERROR
    description = "a mirror must include a rel:'via' link (type text/html) to the original source"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        if provenance_of(node) != "mirror":
            return
        via = [link for link in links_of(node) if link.get("rel") == "via"]
        if not via:
            yield self.finding(
                node,
                "mirror collection has no rel:'via' link to its original source",
                json_pointer="/links",
            )
            return
        for link in via:
            if link.get("type") != "text/html":
                yield self.finding(
                    node,
                    f"rel:'via' link has type {link.get('type')!r}, expected 'text/html'",
                    json_pointer="/links",
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
                json_pointer="/links",
            )


class MirrorUpdatedRule(Rule):
    """A mirror records its last sync time in the top-level updated field.

    Applies to every mirror collection; the root catalog is also required to
    carry it when every collection in the tree is a mirror.
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
        root = graph.root
        collections = list(provenances.values())
        if root is not None and collections and all(p == "mirror" for p in collections):
            yield from self._check_updated(root)

    def _check_updated(self, node: Node) -> Iterable[Finding]:
        value = node.data.get("updated")
        if value is None:
            yield Finding(
                rule_id=self.id,
                severity=self.default_severity,
                message="mirror has no top-level 'updated' field recording the last sync",
                path=str(node.path),
                object_id=node.id,
                json_pointer="/updated",
            )
        elif parse_rfc3339(value) is None:
            yield Finding(
                rule_id=self.id,
                severity=self.default_severity,
                message=f"'updated' value {value!r} is not an RFC 3339 date-time",
                path=str(node.path),
                object_id=node.id,
                json_pointer="/updated",
            )


class OfficialNoUpstreamLinksRule(Rule):
    """An official catalog carries no via/canonical links to an upstream."""

    id = "PTL-PRO-004"
    # No manifest ID: the official-carries-no-upstream-links sentence in
    # core.md, Source Provenance, carries no RFC 2119 keyword.
    default_severity = Severity.ERROR
    description = "an official collection must not carry via or canonical upstream links"
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
                    json_pointer="/links",
                )
