"""Conformance-declaration rules (spec: core.md, Conformance and Versioning)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from reis.catalog import CatalogGraph, Node
from reis.model import Finding, Severity
from reis.rule import Rule

SCHEMA_URI_PATTERN = re.compile(
    r"^https://schemas\.portolan-sdi\.org/portolan/v\d+\.\d+\.\d+/schema\.json$"
)


def declared_schema_uris(node: Node) -> list[str]:
    raw = node.data.get("stac_extensions")
    if not isinstance(raw, list):
        return []
    return [uri for uri in raw if isinstance(uri, str) and SCHEMA_URI_PATTERN.match(uri)]


class SchemaUriDeclaredRule(Rule):
    """Every catalog and collection declares exactly one Portolan schema URI."""

    id = "PTL-CNF-001"
    spec_ids = ("PORTO-CORE-006", "PORTO-CORE-007")
    default_severity = Severity.ERROR
    description = "catalogs and collections must declare the versioned Portolan schema URI"
    kinds = ("catalog", "collection")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        uris = declared_schema_uris(node)
        if not uris:
            yield self.finding(
                node,
                "stac_extensions declares no Portolan schema URI",
                json_pointer="/stac_extensions",
                fix_hint=("add e.g. https://schemas.portolan-sdi.org/portolan/v0.1.0/schema.json"),
            )
        elif len(uris) > 1:
            yield self.finding(
                node,
                f"stac_extensions declares {len(uris)} Portolan schema URIs; exactly one expected",
                json_pointer="/stac_extensions",
            )


class SchemaUriConsistencyRule(Rule):
    """All objects declare the root catalog's schema URI.

    The spec makes declaring the URI a MUST but explicitly downgrades a
    mismatch with the root to a warning: a mixed-version catalog remains
    valid.
    """

    id = "PTL-CNF-002"
    spec_ids = ("PORTO-CORE-009", "PORTO-CORE-010")
    default_severity = Severity.WARNING
    description = "objects whose Portolan schema URI differs from the root's are flagged"
    kinds = ()  # graph-level: compares everything against the root

    def check_graph(self, graph: CatalogGraph) -> Iterable[Finding]:
        root = graph.root
        if root is None:
            return
        root_uris = declared_schema_uris(root)
        if len(root_uris) != 1:
            return  # PTL-CNF-001 reports the root's own problem
        root_uri = root_uris[0]
        for node in graph.iter("catalog", "collection"):
            if node is root:
                continue
            uris = declared_schema_uris(node)
            if len(uris) != 1:
                continue  # PTL-CNF-001 reports missing/ambiguous declarations
            if uris[0] != root_uri:
                yield Finding(
                    rule_id=self.id,
                    severity=self.default_severity,
                    message=(
                        f"declared schema URI {uris[0]} differs from the root catalog's {root_uri}"
                    ),
                    path=str(node.path),
                    object_id=node.id,
                    json_pointer="/stac_extensions",
                )


# The STAC version extension, the one place dataset versioning may live.
# Version-tolerant prefix, matching how other extension declarations are
# checked (web-map-links in viz, partition in partitions).
_VERSION_EXTENSION_PREFIX = "https://stac-extensions.github.io/version/"

# Fields the version extension defines on catalogs, collections, and items.
_VERSION_FIELDS = ("version", "deprecated", "experimental")


class VersionExtensionRule(Rule):
    """Dataset versioning, where used, declares the STAC version extension.

    core.md, Conformance and Versioning: "The specification version MUST NOT
    be conflated with the dataset version. Dataset versioning, where used,
    MUST use the STAC version extension." The specification version's only
    home is the Portolan schema URI (PTL-CNF-001); a dataset version's only
    home is the version extension's fields, and using those fields without
    declaring the extension's schema in ``stac_extensions`` is the
    machine-checkable violation.
    """

    id = "PTL-CNF-003"
    spec_ids = ("PORTO-CORE-007", "PORTO-CORE-008")
    default_severity = Severity.ERROR
    description = "dataset versioning must declare the STAC version extension"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        container = node.data.get("properties") if node.kind == "item" else node.data
        if not isinstance(container, dict):
            return
        used = [field for field in _VERSION_FIELDS if field in container]
        if not used or _declares_version_extension(node):
            return
        prefix = "/properties/" if node.kind == "item" else "/"
        yield self.finding(
            node,
            f"dataset version field(s) {used} used without declaring the STAC"
            " version extension in stac_extensions",
            json_pointer=f"{prefix}{used[0]}",
            fix_hint=f"add '{_VERSION_EXTENSION_PREFIX}v1.2.0/schema.json' to stac_extensions",
        )


def _declares_version_extension(node: Node) -> bool:
    extensions = node.data.get("stac_extensions")
    if not isinstance(extensions, list):
        return False
    return any(
        isinstance(uri, str) and uri.startswith(_VERSION_EXTENSION_PREFIX) for uri in extensions
    )
