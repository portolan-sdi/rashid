"""The Portolan profile versions this build bundles, and the spec commit behind them.

It reads only the package data directory, so the CLI, the rules, and the
schema pass can ask which version is newest without loading a schema.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from rashid import _json, _package
from rashid.catalog import Node

PROFILE_URI_PREFIX = "https://schemas.portolan-sdi.org/portolan/"
_PROFILE_URI = re.compile(
    r"https://schemas\.portolan-sdi\.org/portolan/(v[0-9]+\.[0-9]+\.[0-9]+)/schema\.json"
)


def profile_version_key(version: str) -> tuple[int, ...]:
    """Sort key for a ``vX.Y.Z`` version: numeric, so v0.10.0 follows v0.9.0."""
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def profile_uri(version: str) -> str:
    """The schema URI a catalog declares for ``version``."""
    return f"{PROFILE_URI_PREFIX}{version}/schema.json"


def profile_version(uri: str) -> str | None:
    """The ``vX.Y.Z`` in a profile schema URI, or None for another URI."""
    match = _PROFILE_URI.fullmatch(uri)
    return match.group(1) if match else None


@lru_cache(maxsize=1)
def bundled_profile_versions() -> tuple[str, ...]:
    """The profile schema versions in the wheel, oldest first."""
    root = _package.schemas().joinpath("portolan")
    found = [d.name for d in root.iterdir() if d.joinpath("schema.json").is_file()]
    return tuple(sorted(found, key=profile_version_key))


def newest_profile_version() -> str:
    """The newest bundled profile version: the requirements rashid applies."""
    return bundled_profile_versions()[-1]


@lru_cache(maxsize=1)
def spec_commit() -> str | None:
    """The portolan-spec commit the bundled schemas and fixtures came from."""
    ref = _package.schemas().joinpath("spec-ref.json")
    if not ref.is_file():
        return None
    commit = _json.loads(ref.read_bytes()).get("commit")
    return commit if isinstance(commit, str) else None


SCHEMA_URI_PATTERN = re.compile(
    r"^https://schemas\.portolan-sdi\.org/portolan/v\d+\.\d+\.\d+/schema\.json$"
)


def declared_schema_uris(node: Node) -> list[str]:
    raw = node.data.get("stac_extensions")
    if not isinstance(raw, list):
        return []
    return [uri for uri in raw if isinstance(uri, str) and SCHEMA_URI_PATTERN.match(uri)]


# The profile's extension registry, vendored from the spec's stac/README.md by
# scripts/vendor_spec_fixtures.py and shipped in the wheel.
_REGISTRY_RESOURCE = "extension-registry.json"


# Every registered URI pins a version: <base>/vX.Y.Z/schema.json. The base
# has the host and the extension's path, so it identifies the extension
# across versions on either host the registry uses.
_EXTENSION_URI_PATTERN = re.compile(
    r"^(?P<base>https://\S+?)/(?P<version>v\d+\.\d+\.\d+)/schema\.json$"
)


@lru_cache(maxsize=1)
def _registry() -> dict[str, tuple[str, str]]:
    """Registered extensions, keyed by base URI: base -> (name, pinned version)."""
    resource = _package.schemas().joinpath(_REGISTRY_RESOURCE)
    document: dict[str, Any] = _json.loads(resource.read_bytes())
    registered: dict[str, tuple[str, str]] = {}
    for name, uri in document["extensions"].items():
        parsed = _EXTENSION_URI_PATTERN.match(str(uri))
        if parsed is None:  # pragma: no cover - the vendoring script rejects these
            continue
        registered[parsed["base"]] = (str(name), parsed["version"])
    return registered


def registered_extension(uri: str) -> tuple[str, str] | None:
    """``(name, version)`` when ``uri`` is a registered extension's pinned URI.

    ``None`` covers a URI that is not a versioned schema URI, an extension the
    registry does not list, and a registered extension declared at a version
    the registry does not pin. :func:`registered_at_other_version` tells the
    last case apart.
    """
    parsed = _EXTENSION_URI_PATTERN.match(uri)
    if parsed is None:
        return None
    entry = _registry().get(parsed["base"])
    if entry is None or entry[1] != parsed["version"]:
        return None
    return entry


def registered_at_other_version(uri: str) -> tuple[str, str] | None:
    """``(name, declared version)`` when ``uri`` is a registered extension at an unpinned version.

    A catalog declaring Raster v1.1.0 must not be validated against the pinned
    v2.0.0 schema, which invents errors about fields v1.1.0 never had. It must
    not escape validation either, or declaring an older version would turn
    every schema error into a PTL-CNF-004 warning. The extension pass uses this
    to validate such objects against the schema of the version they declare.
    """
    parsed = _EXTENSION_URI_PATTERN.match(uri)
    if parsed is None:
        return None
    entry = _registry().get(parsed["base"])
    if entry is None or entry[1] == parsed["version"]:
        return None
    return entry[0], parsed["version"]
