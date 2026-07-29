"""Schema pass: Portolan profile validation, delegated to the published schema.

The metadata pass (:mod:`rashid.rules`) checks Portolan requirements by hand over
raw JSON, deliberately stdlib-only, so it can emit precise per-rule findings with
fix hints. The Portolan STAC Profile publishes those same requirements as a
single machine-checkable JSON Schema — the spec calls it "the machine-checkable
core of the metadata pass". This pass applies that published schema directly to
every object, exactly as the profile's own ``check-portolan`` test does: an
authoritative oracle that catches any drift between rashid's hand rules and the
canonical schema, and any requirement the hand rules do not yet cover.

Because it overlaps the hand rules by design, the pass is opt-in (CLI
``--schema``; ``validate(..., schema=True)``). When it runs, a defect may be
reported twice — once by a hand rule with a fix hint, once here with the schema's
own message; the second is the canonical verdict.

The published schema versions ship inside the wheel (``rashid/_schemas/portolan``),
so the default validator resolves the URI the root catalog declares against the
bundled copies and runs offline. The network is reached only for a schema version
this build does not carry, and only when the caller passes ``allow_network=True``;
without it, an unbundled version downgrades to a single WARNING so the offline
metadata findings still surface. The validator is injectable so tests can
substitute their own.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from functools import lru_cache
from importlib import resources
from typing import Any

from rashid._http import user_agent
from rashid._jsonschema import SchemaError
from rashid.catalog import CatalogGraph, Kind
from rashid.model import Finding, Severity
from rashid.rules.conformance import declared_schema_uris

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

# A validator maps one object's raw JSON to a list of schema errors;
# an empty list means the object satisfies the Portolan profile schema.
Validator = Callable[[dict[str, Any]], list[SchemaError]]

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


def _fetch_schema(schema_uri: str) -> dict[str, Any]:
    # Only fetch over https: the URI can originate from a catalog's declared
    # stac_extensions, so a file:// or custom scheme would let a hostile catalog
    # read local files or reach internal hosts (CWE-22 / SSRF).
    if not schema_uri.startswith("https://"):
        raise ValueError(f"schema URI must be an https URL, got: {schema_uri!r}")
    # Named agent for the same reason the live pass sends one: the URI can come
    # from the catalog's own stac_extensions, so the schema may sit behind a CDN
    # that answers the default Python-urllib agent with a 403.
    request = urllib.request.Request(  # noqa: S310  # nosec B310 - scheme checked above
        schema_uri, headers={"User-Agent": user_agent()}
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # nosec B310 - scheme checked above
        schema: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    return schema


@lru_cache(maxsize=1)
def _bundled_schemas() -> dict[str, dict[str, Any]]:
    """Every profile schema shipped in the wheel, keyed by its ``$id`` sans ``#``.

    Versions are discovered from ``rashid/_schemas/portolan/<vX.Y.Z>/schema.json``,
    never hardcoded, so a re-vendor picks up a new spec release with no code
    change (mirroring the fixture-discovery convention in
    ``tests/unit/test_schema_uri_invariant.py``).
    """
    schemas: dict[str, dict[str, Any]] = {}
    root = resources.files("rashid").joinpath("_schemas/portolan")
    for version_dir in root.iterdir():
        schema_file = version_dir.joinpath("schema.json")
        if not schema_file.is_file():
            continue
        schema: dict[str, Any] = json.loads(schema_file.read_text(encoding="utf-8"))
        schemas[str(schema["$id"]).rstrip("#")] = schema
    return schemas


def bundled_schema_versions() -> tuple[str, ...]:
    """The profile schema versions this build carries, e.g. ``("v0.1.0",)``."""
    versions = (uri.split("/portolan/")[1].split("/")[0] for uri in _bundled_schemas())
    return tuple(sorted(versions, key=lambda v: tuple(int(p) for p in v.lstrip("v").split("."))))


def bundled_schema(schema_uri: str) -> dict[str, Any] | None:
    """The bundled profile schema whose ``$id`` matches ``schema_uri``, if carried."""
    return _bundled_schemas().get(schema_uri.rstrip("#"))


def validator_from_schema(schema: dict[str, Any]) -> Validator:
    """Build the ``jsonschema``-backed Portolan profile validator from a schema.

    Split from :func:`default_validator` so the schema can come from anywhere —
    the bundled copies, a fetched document, or a test's inline dict.
    ``jsonschema`` is imported lazily because the metadata pass never needs it.
    Draft-07 is pinned because the published Portolan schema declares
    ``$schema`` draft-07. Error narrowing (the oneOf discriminator dance) lives
    in :func:`rashid._jsonschema.describe`, shared with the structural pass.
    """
    from jsonschema import Draft7Validator

    from rashid._jsonschema import describe

    validator = Draft7Validator(schema)

    def _validate(data: dict[str, Any]) -> list[SchemaError]:
        return [describe(error) for error in sorted(validator.iter_errors(data), key=str)]

    return _validate


def default_validator(
    schema_uri: str = DEFAULT_SCHEMA_URI, *, allow_network: bool = False
) -> Validator:
    """Build the profile validator for ``schema_uri``, offline first.

    A bundled copy of the schema (shipped in the wheel) is preferred. For a
    version this build does not carry, ``allow_network=True`` fetches it from
    ``schema_uri``; without it a ``LookupError`` names the bundled versions.
    Callers with a local schema in hand can build a validator directly with
    :func:`validator_from_schema`.
    """
    schema = bundled_schema(schema_uri)
    if schema is not None:
        return validator_from_schema(schema)
    if allow_network:
        return validator_from_schema(_fetch_schema(schema_uri))
    raise LookupError(
        f"no bundled schema for {schema_uri}; this build carries "
        f"{', '.join(bundled_schema_versions())} (pass allow_network=True to fetch)"
    )


def validate_schema(
    graph: CatalogGraph,
    validator: Validator | None = None,
    schema_uri: str | None = None,
    *,
    allow_network: bool = False,
) -> list[Finding]:
    """Validate every catalog, collection, and item against the profile schema.

    Returns ``PTL-SCH-001`` errors for each schema failure. The schema comes
    from the bundled copies in the wheel; a version this build does not carry
    is fetched over the network only when ``allow_network`` is set. If the
    validator is unavailable (missing package, unbundled version offline, or a
    failed fetch), returns a single ``PTL-SCH-000`` warning instead of failing
    the run — a systemic failure is reported once, not once per object.
    """
    if validator is None:
        uri = schema_uri if schema_uri is not None else _schema_uri_for(graph)
        try:
            validator = default_validator(uri, allow_network=allow_network)
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
                    message=f"Portolan profile schema validation failed: {error.message}",
                    path=str(node.path),
                    object_id=node.id,
                    json_pointer=error.json_pointer,
                )
            )
    return findings
