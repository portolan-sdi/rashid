"""Schema pass: Portolan profile validation, delegated to the published schema.

The metadata pass (:mod:`reis.rules`) checks Portolan requirements by hand over
raw JSON, deliberately stdlib-only, so it can emit precise per-rule findings with
fix hints. The Portolan STAC Profile publishes those same requirements as a
single machine-checkable JSON Schema — the spec calls it "the machine-checkable
core of the metadata pass". This pass applies that published schema directly to
every object, exactly as the profile's own ``check-portolan`` test does: an
authoritative oracle that catches any drift between reis's hand rules and the
canonical schema, and any requirement the hand rules do not yet cover.

Because it overlaps the hand rules by design, the pass is opt-in (CLI
``--schema``; ``validate(..., schema=True)``). When it runs, a defect may be
reported twice — once by a hand rule with a fix hint, once here with the schema's
own message; the second is the canonical verdict.

The validator is injectable so the pass can be exercised offline in tests. The
default implementation fetches the schema over the network (cached in-process)
from the URI the root catalog declares, falling back to the pinned v0.1 URI. A
validator that cannot run — the ``jsonschema`` package is absent, or the schema
is unreachable — downgrades to a single WARNING so the offline metadata findings
still surface.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from reis.catalog import CatalogGraph, Kind
from reis.model import Finding, Severity
from reis.rules.conformance import declared_schema_uris

SCH_INVALID = "PTL-SCH-001"
SCH_UNAVAILABLE = "PTL-SCH-000"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
SPEC_IDS: dict[str, tuple[str, ...]] = {
    # The published profile schema is the machine-checkable aggregate of
    # the same requirements the metadata rules cite individually; the
    # declared-URI MUST is the one it enforces beyond that overlap.
    SCH_INVALID: ("PORTO-CORE-006",),
}

# The pinned v0.1 profile schema, used when the root declares no single URI.
DEFAULT_SCHEMA_URI = "https://schemas.portolan-sdi.org/portolan/v0.1.0/schema.json"

# A validator maps one object's raw JSON to a list of schema-error messages;
# an empty list means the object satisfies the Portolan profile schema.
Validator = Callable[[dict[str, Any]], list[str]]

_SCHEMA_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")


def _schema_uri_for(graph: CatalogGraph) -> str:
    """The schema URI to validate against: the root's declared one, or the default.

    The profile schema is a single document (``oneOf`` Catalog/Collection/Item),
    so one URI validates every object in the tree. A malformed or ambiguous root
    declaration is the metadata pass's concern (``PTL-CNF-001``); here it simply
    falls back to the pinned default rather than failing the schema pass.
    """
    if graph.root is not None:
        uris = declared_schema_uris(graph.root)
        if len(uris) == 1:
            return uris[0]
    return DEFAULT_SCHEMA_URI


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


def _fetch_schema(schema_uri: str) -> dict[str, Any]:
    # Only fetch over https: the URI can originate from a catalog's declared
    # stac_extensions, so a file:// or custom scheme would let a hostile catalog
    # read local files or reach internal hosts (CWE-22 / SSRF).
    if not schema_uri.startswith("https://"):
        raise ValueError(f"schema URI must be an https URL, got: {schema_uri!r}")
    with urllib.request.urlopen(schema_uri, timeout=30) as response:  # noqa: S310  # nosec B310 - scheme checked above
        schema: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return schema


def validator_from_schema(schema: dict[str, Any]) -> Validator:
    """Build the ``jsonschema``-backed Portolan profile validator from a schema.

    Split from :func:`default_validator` so the schema can come from disk — the
    hermetic tests validate the vendored reference catalog against the vendored
    profile schema with no network. ``jsonschema`` is imported lazily because the
    metadata pass never needs it. Draft-07 is pinned because the published
    Portolan schema declares ``$schema`` draft-07.
    """
    from jsonschema import Draft7Validator
    from jsonschema.exceptions import best_match

    validator = Draft7Validator(schema)

    def _describe(error: Any) -> str:
        # The profile schema is a top-level oneOf discriminated by `type`
        # (Catalog/Collection/Item). A raw oneOf failure reports the whole
        # instance and best_match may drill into the wrong branch — telling a
        # Collection it "was expected" to be a Catalog. Restrict to the branch
        # whose `type` the object matched (the one with no discriminator error),
        # then surface its most relevant cause.
        while error.context:
            error = best_match(_matched_branch(error.context))
        # RFC 6901 JSON pointer: "" at the document root, "/links/0/type" below.
        pointer = "".join(f"/{part}" for part in error.absolute_path)
        message = error.message
        if len(message) > 300:
            message = message[:297] + "..."
        return f"{message} (at {pointer})"

    def _validate(data: dict[str, Any]) -> list[str]:
        return [_describe(error) for error in sorted(validator.iter_errors(data), key=str)]

    return _validate


def default_validator(schema_uri: str = DEFAULT_SCHEMA_URI) -> Validator:
    """Build the profile validator by fetching the schema over the network.

    The first use fetches the profile schema from ``schema_uri`` (the URI the
    root catalog declares, or the pinned default). Offline callers should build
    a validator with :func:`validator_from_schema` from a local schema instead.
    """
    return validator_from_schema(_fetch_schema(schema_uri))


def validate_schema(
    graph: CatalogGraph,
    validator: Validator | None = None,
    schema_uri: str | None = None,
) -> list[Finding]:
    """Validate every catalog, collection, and item against the profile schema.

    Returns ``PTL-SCH-001`` errors for each schema failure. If the validator is
    unavailable (missing package) or a call fails (typically the schema fetch
    could not reach the network), returns a single ``PTL-SCH-000`` warning
    instead of failing the run — a systemic failure is reported once, not once
    per object.
    """
    if validator is None:
        uri = schema_uri if schema_uri is not None else _schema_uri_for(graph)
        try:
            validator = default_validator(uri)
        except ImportError:
            return [
                Finding(
                    rule_id=SCH_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=(
                        "schema validation skipped: the 'jsonschema' package is not installed"
                    ),
                    path=".",
                )
            ]
        except Exception as exc:  # noqa: BLE001 - schema fetch/compile failure is systemic
            return [
                Finding(
                    rule_id=SCH_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=f"schema validation could not run: {exc}",
                    path=".",
                )
            ]

    findings: list[Finding] = []
    for node in graph.iter(*_SCHEMA_KINDS):
        if node.parse_error is not None:
            continue
        try:
            errors = validator(node.data)
        except Exception as exc:  # noqa: BLE001 - any validator failure is systemic
            findings.append(
                Finding(
                    rule_id=SCH_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=f"schema validation could not run: {exc}",
                    path=".",
                )
            )
            return findings
        for error in errors:
            findings.append(
                Finding(
                    rule_id=SCH_INVALID,
                    severity=Severity.ERROR,
                    message=f"Portolan profile schema validation failed: {error}",
                    path=str(node.path),
                    object_id=node.id,
                )
            )
    return findings
