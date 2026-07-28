"""Provider rules (spec: core.md, Providers)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule


def providers_of(node: Node) -> list[dict[str, Any]]:
    raw = node.data.get("providers")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, dict)]


def _roles(provider: dict[str, Any]) -> list[str]:
    raw = provider.get("roles")
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str)]


class ProducerPresentRule(Rule):
    """At least one provider carries the producer role."""

    id = "PTL-PRV-001"
    spec_ids = ("PORTO-CORE-046", "PORTO-CORE-047")
    default_severity = Severity.ERROR
    description = "every collection needs at least one provider with the producer role"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        providers = providers_of(node)
        if not providers:
            yield self.finding(
                node,
                "collection declares no providers",
                json_pointer="/providers",
                fix_hint='add a providers array, e.g. [{"name": "Statistics Agency", "roles":'
                ' ["producer", "host"], "url": "https://example.org"}]',
            )
            return
        if not any("producer" in _roles(p) for p in providers):
            yield self.finding(
                node,
                "no provider carries the 'producer' role",
                json_pointer="/providers",
                fix_hint='add "producer" to the roles of the organization that made the data,'
                " or add that organization as a new provider",
            )


class SingleHostRule(Rule):
    """Exactly one host provider, listed last."""

    id = "PTL-PRV-002"
    spec_ids = ("PORTO-CORE-047",)
    default_severity = Severity.ERROR
    description = "exactly one provider with the host role, listed as the last element"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        providers = providers_of(node)
        if not providers:
            return  # PTL-PRV-001 reports the absence
        hosts = [p for p in providers if "host" in _roles(p)]
        if len(hosts) != 1:
            yield self.finding(
                node,
                f"expected exactly one provider with the 'host' role, found {len(hosts)}",
                json_pointer="/providers",
                fix_hint='leave the "host" role on exactly one provider, the organization'
                " serving these files, and drop it from the rest",
                expected=1,
                actual=len(hosts),
            )
            return
        if "host" not in _roles(providers[-1]):
            yield self.finding(
                node,
                "the host provider must be the last element of the providers list",
                json_pointer="/providers",
                fix_hint="move the host provider to the end of the providers array",
            )


class HostContactRule(Rule):
    """The host provider is reachable through a url or an email."""

    id = "PTL-PRV-003"
    spec_ids = ("PORTO-CORE-051",)
    default_severity = Severity.ERROR
    description = "the host provider must include a url or an email"
    kinds = ("collection",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        hosts = [p for p in providers_of(node) if "host" in _roles(p)]
        if len(hosts) != 1:
            return  # PTL-PRV-002 reports host-count problems
        host = hosts[0]
        url = host.get("url")
        email = host.get("email")
        has_url = isinstance(url, str) and url.strip()
        has_email = isinstance(email, str) and email.strip()
        if not has_url and not has_email:
            yield self.finding(
                node,
                f"host provider '{host.get('name', '?')}' has neither a url nor an email",
                json_pointer="/providers",
                fix_hint="add a url to a maintainer contact page, or an email field",
            )
