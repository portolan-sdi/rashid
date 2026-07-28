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
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from importlib.abc import Traversable

    from referencing import Registry

STAC_VERSION = "1.1.0"
_STAC_BASE = f"https://schemas.stacspec.org/v{STAC_VERSION}/"
_GEOJSON_BASE = "https://geojson.org/schema/"


@dataclass(frozen=True)
class SchemaError:
    """One schema violation: a message and the RFC 6901 pointer it applies to."""

    message: str
    json_pointer: str

    def __str__(self) -> str:
        return f"{self.message} (at {self.json_pointer})"


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


@lru_cache(maxsize=1)
def stac_registry() -> Registry:
    """A ``referencing.Registry`` over the vendored STAC + GeoJSON closure.

    Each resource is keyed by its canonical retrieval URL — what the schemas'
    own absolute ``$ref``s point at — and its ``$id`` is normalized to that URL
    so relative ``$ref``s resolve against it rather than a possibly-malformed
    upstream ``$id``. Cached: the vendored closure never changes at runtime.
    """
    from referencing import Registry, Resource

    schemas = resources.files("rashid").joinpath("_schemas")
    registry: Registry = Registry()
    trees = (
        (schemas.joinpath("stac").joinpath(STAC_VERSION), _STAC_BASE),
        (schemas.joinpath("geojson"), _GEOJSON_BASE),
    )
    for root, base in trees:
        for rel_path, text in _iter_vendored(root):
            url = f"{base}{rel_path}"
            contents: dict[str, Any] = json.loads(text)
            contents["$id"] = url
            registry = registry.with_resource(uri=url, resource=Resource.from_contents(contents))
    return registry.crawl()


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
    if len(message) > 300:
        message = message[:297] + "..."
    return SchemaError(message=message, json_pointer=pointer)
