"""Conformance-declaration rules (spec: core.md, Conformance and Versioning)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from functools import lru_cache
from importlib import resources
from typing import Any

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule

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
                fix_hint=("add e.g. https://schemas.portolan-sdi.org/portolan/v0.1.2/schema.json"),
            )
        elif len(uris) > 1:
            yield self.finding(
                node,
                f"stac_extensions declares {len(uris)} Portolan schema URIs; exactly one expected",
                json_pointer="/stac_extensions",
                fix_hint="keep the one Portolan schema URI this object conforms to and remove"
                " the others from stac_extensions",
                expected=1,
                actual=len(uris),
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
                    fix_hint=(
                        f"replace the declared URI with the root catalog's {root_uri}, or"
                        " migrate the whole catalog to one schema version"
                    ),
                    expected=root_uri,
                    actual=uris[0],
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


# The profile's extension registry, vendored from the spec's stac/README.md by
# scripts/vendor_spec_fixtures.py and shipped in the wheel.
_REGISTRY_RESOURCE = "_schemas/extension-registry.json"

# Every registered URI pins a version: <base>/vX.Y.Z/schema.json. The base
# carries the host and the extension's path, so it identifies the extension
# across versions on either host the registry uses.
_EXTENSION_URI_PATTERN = re.compile(
    r"^(?P<base>https://\S+?)/(?P<version>v\d+\.\d+\.\d+)/schema\.json$"
)


@lru_cache(maxsize=1)
def _registry() -> dict[str, tuple[str, str]]:
    """Registered extensions, keyed by base URI: base -> (name, pinned version)."""
    resource = resources.files("rashid").joinpath(_REGISTRY_RESOURCE)
    document: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
    registered: dict[str, tuple[str, str]] = {}
    for name, uri in document["extensions"].items():
        parsed = _EXTENSION_URI_PATTERN.match(str(uri))
        if parsed is None:  # pragma: no cover - the vendoring script rejects these
            continue
        registered[parsed["base"]] = (str(name), parsed["version"])
    return registered


def registered_extension(uri: str) -> tuple[str, str] | None:
    """``(name, version)`` when ``uri`` is a registered extension's pinned URI.

    ``None`` covers three cases the extension pass treats alike: a URI that is
    not a versioned schema URI, an extension the registry does not list, and a
    registered extension declared at a version the registry no longer pins.
    The last matters most. A catalog declaring Raster v1.1.0 must not be
    validated against the pinned v2.0.0 schema — that invents errors about
    fields the older version never had. ``PTL-CNF-004`` already reports the
    version itself; the extension pass just declines to guess.
    """
    parsed = _EXTENSION_URI_PATTERN.match(uri)
    if parsed is None:
        return None
    entry = _registry().get(parsed["base"])
    if entry is None or entry[1] != parsed["version"]:
        return None
    return entry


class ExtensionVersionRule(Rule):
    """A registered extension is declared at the version the registry pins.

    The profile's STAC extension registry (the spec's ``stac/README.md``) is
    the normative list of which extensions Portolan uses and which schema URI
    to pin for each. core.md defers to it: "Which extensions are required
    versus recommended, the exact schema URIs and versions to pin, and their
    usage are defined normatively by the Portolan STAC Profile." That sentence
    carries no RFC 2119 keyword, so the requirements manifest assigns the
    registry no ID and this rule cites none.

    The finding is a warning rather than an error. The registry advances after
    catalogs are published, and a catalog that pinned the then-current version
    is not broken, only behind.

    Extensions absent from the registry are ignored: the registry governs what
    it lists, not what a publisher adds. The Portolan schema URI is ignored
    too, because the version a catalog declares there is owned by PTL-CNF-001
    and PTL-CNF-002, which the spec pins to a warning about the root catalog
    rather than about the registry.
    """

    id = "PTL-CNF-004"
    spec_ids = ()
    default_severity = Severity.WARNING
    description = "declared extension URIs match the versions the profile registry pins"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        declared = node.data.get("stac_extensions")
        if not isinstance(declared, list):
            return
        registry = _registry()
        for index, uri in enumerate(declared):
            if not isinstance(uri, str) or SCHEMA_URI_PATTERN.match(uri):
                continue
            parsed = _EXTENSION_URI_PATTERN.match(uri)
            if parsed is None:
                continue  # not a versioned schema URI; nothing to compare
            entry = registry.get(parsed["base"])
            if entry is None:
                continue  # unregistered extension: out of the registry's scope
            name, pinned = entry
            if parsed["version"] == pinned:
                continue
            expected = f"{parsed['base']}/{pinned}/schema.json"
            yield self.finding(
                node,
                f"{name} extension declared at {parsed['version']}, but the Portolan"
                f" extension registry pins {pinned}",
                json_pointer=f"/stac_extensions/{index}",
                fix_hint=f"replace '{uri}' with '{expected}' in stac_extensions",
                expected=expected,
                actual=uri,
            )
