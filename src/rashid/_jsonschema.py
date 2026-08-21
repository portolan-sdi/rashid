"""Shared JSON Schema support for the structural and schema passes.

Both passes compile a draft-07 schema with ``jsonschema`` and need the same two
services: a registry that resolves the vendored STAC/GeoJSON closure offline,
and an error formatter that turns a raw ``ValidationError`` into one precise
:class:`SchemaError` instead of a wall of ``oneOf`` noise.

The vendored files on disk stay byte-identical to upstream so the vendoring
script's ``--check`` stays an honest diff; the ``$id`` normalization that
resolution needs (upstream ``common.json`` ships a malformed ``$id``) happens
here at load time. ``jsonschema`` is imported lazily by the callers, never at
module import — the metadata pass must stay stdlib-only.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from importlib.abc import Traversable

    from referencing import Registry

STAC_VERSION = "1.1.0"
_STAC_HOST = "https://schemas.stacspec.org/"
_STAC_BASE = f"{_STAC_HOST}v{STAC_VERSION}/"
_GEOJSON_BASE = "https://geojson.org/schema/"


@dataclass(frozen=True)
class SchemaError:
    """One schema violation: a message and the RFC 6901 pointer it applies to."""

    message: str
    json_pointer: str

    def __str__(self) -> str:
        return f"{self.message} (at {self.json_pointer})"


def fetch_schema(schema_uri: str) -> dict[str, Any]:
    """Fetch one schema over https.

    Shared by the profile and extension passes, which both accept a schema URI
    that originated in a catalog's ``stac_extensions``. Only https is allowed:
    a ``file://`` or custom scheme would let a hostile catalog read local files
    or reach internal hosts (CWE-22 / SSRF). The named agent is sent for the
    same reason the live pass sends one — a schema behind a CDN may answer the
    default Python-urllib agent with a 403.
    """
    from rashid._http import user_agent

    if not schema_uri.startswith("https://"):
        raise ValueError(f"schema URI must be an https URL, got: {schema_uri!r}")
    request = urllib.request.Request(  # noqa: S310  # nosec B310 - scheme checked above
        schema_uri, headers={"User-Agent": user_agent()}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310 - scheme checked above
        schema: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return schema


def _iter_vendored(node: Traversable, prefix: str = "") -> list[tuple[str, str]]:
    """Yield (relative-path, text) for every vendored ``.json`` under ``node``."""
    out: list[tuple[str, str]] = []
    for child in node.iterdir():
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            out.extend(_iter_vendored(child, prefix=f"{rel}/"))
        elif child.name.endswith(".json"):
            out.append((rel, child.read_text(encoding="utf-8")))
    return out


def _registry_over(trees: tuple[tuple[Traversable, str], ...]) -> Registry:
    """Build a ``referencing.Registry`` over vendored trees.

    Each resource is keyed by its canonical retrieval URL — what the schemas'
    own absolute ``$ref``s point at — and its ``$id`` is normalized to that URL
    so relative ``$ref``s resolve against it rather than a possibly-malformed
    upstream ``$id``.
    """
    from referencing import Registry, Resource

    registry: Registry = Registry()
    for root, base in trees:
        for rel_path, text in _iter_vendored(root):
            url = f"{base}{rel_path}"
            contents: dict[str, Any] = json.loads(text)
            contents["$id"] = url
            registry = registry.with_resource(uri=url, resource=Resource.from_contents(contents))
    return registry.crawl()


@lru_cache(maxsize=1)
def stac_registry() -> Registry:
    """A registry over the vendored STAC 1.1.0 + GeoJSON closure.

    Scoped to what the structural pass resolves. Cached: the vendored closure
    never changes at runtime.
    """
    schemas = resources.files("rashid").joinpath("_schemas")
    return _registry_over(
        (
            (schemas.joinpath("stac").joinpath(STAC_VERSION), _STAC_BASE),
            (schemas.joinpath("geojson"), _GEOJSON_BASE),
        )
    )


@lru_cache(maxsize=1)
def extension_registry() -> Registry:
    """A registry over every vendored tree, for the extension pass.

    Extension schemas reach wider than the core closure: PROJJSON on proj.org,
    STAC 1.0.0 ``basics.json``, and the GeoJSON geometry model. Rather than
    enumerate those, walk what the vendoring script wrote — every STAC version
    present, GeoJSON, and each ``extensions/<host>/`` tree — and rebuild each
    file's URL from its path. A schema vendored by a future spec sync resolves
    with no change here.
    """
    schemas = resources.files("rashid").joinpath("_schemas")
    trees: list[tuple[Traversable, str]] = [(schemas.joinpath("geojson"), _GEOJSON_BASE)]
    for version_dir in schemas.joinpath("stac").iterdir():
        if version_dir.is_dir():
            trees.append((version_dir, f"{_STAC_HOST}v{version_dir.name}/"))
    extensions = schemas.joinpath("extensions")
    if extensions.is_dir():
        for host_dir in extensions.iterdir():
            if host_dir.is_dir():
                trees.append((host_dir, f"https://{host_dir.name}/"))
    return _registry_over(tuple(trees))


@lru_cache(maxsize=1)
def vendored_extension_schemas() -> dict[str, dict[str, Any]]:
    """The vendored ``extensions/`` tree as a URI -> schema store.

    A plain store, not a policy: it answers "do we carry this URI offline?".
    Which URIs an object may legitimately declare is the profile registry's
    call, and :func:`rashid.rules.conformance.registered_extension` makes it.
    The store therefore also holds schemas that are only ``$ref`` targets,
    such as PROJJSON, which no object ever declares.
    """
    schemas = resources.files("rashid").joinpath("_schemas").joinpath("extensions")
    if not schemas.is_dir():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for host_dir in schemas.iterdir():
        if not host_dir.is_dir():
            continue
        for rel_path, text in _iter_vendored(host_dir):
            out[f"https://{host_dir.name}/{rel_path}"] = json.loads(text)
    return out


def _is_discriminator(error: Any) -> bool:
    """True when ``error`` is a ``type`` const/enum failure — the oneOf discriminator."""
    return error.validator in {"const", "enum"} and list(error.absolute_path) == ["type"]


def _matched_branch(context: list[Any]) -> list[Any]:
    """Restrict a oneOf's sub-errors to the branch the object's ``type`` selected.

    Each sub-error's ``schema_path`` begins with its branch index. A branch that
    failed only on the ``type`` discriminator is the wrong shape (e.g. the
    Catalog branch for a Collection object); the matched branch is the one that
    produced no discriminator error. Returns that branch's errors, or the whole
    context if the discriminator matched no branch (an unknown ``type``).
    """
    by_branch: dict[Any, list[Any]] = {}
    for error in context:
        branch = error.schema_path[0] if error.schema_path else None
        by_branch.setdefault(branch, []).append(error)
    matched = [errors for errors in by_branch.values() if not any(map(_is_discriminator, errors))]
    return min(matched, key=len) if matched else list(context)


_MAX_MESSAGE = 300


def _contains_requirement(subschema: Any) -> str | None:
    """A short phrase for what a ``contains`` subschema demands, if expressible.

    ``jsonschema`` renders a failed ``contains`` as "None of [<the whole
    array>] are valid under the given schema" — it prints the haystack and
    never the needle. For the shapes STAC extensions actually use, the needle
    is a ``const`` or ``enum``, so say that instead.
    """
    if not isinstance(subschema, dict):
        return None

    def _values(spec: dict[str, Any]) -> str | None:
        if "const" in spec:
            return repr(spec["const"])
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            return " or ".join(repr(value) for value in enum)
        return None

    properties = subschema.get("properties")
    if isinstance(properties, dict):
        parts = [
            f"{name} of {rendered}"
            for name, spec in properties.items()
            if isinstance(spec, dict) and (rendered := _values(spec)) is not None
        ]
        if parts:
            return "; ".join(parts)
    return _values(subschema)


def describe(error: Any) -> SchemaError:
    """Collapse a ``jsonschema.ValidationError`` into one precise SchemaError.

    Type-discriminated ``oneOf``/``anyOf`` failures report the whole instance,
    and ``best_match`` may drill into the wrong branch — telling a Collection it
    "was expected" to be a Catalog. Restrict to the branch whose ``type`` the
    object matched (the one with no discriminator error), then surface its most
    relevant cause. Messages are truncated: a schema error over a large object
    can embed the whole instance.
    """
    from jsonschema.exceptions import best_match

    while error.context:
        error = best_match(_matched_branch(error.context))
    # RFC 6901 JSON pointer: "" at the document root, "/links/0/type" below.
    pointer = "".join(f"/{part}" for part in error.absolute_path)
    message = error.message
    if error.validator == "contains":
        requirement = _contains_requirement(error.validator_value)
        if requirement is not None:
            count = len(error.instance) if isinstance(error.instance, list) else 0
            message = f"none of the {count} entries has {requirement}"
    if len(message) > _MAX_MESSAGE:
        message = message[: _MAX_MESSAGE - 3] + "..."
    return SchemaError(message=message, json_pointer=pointer)
