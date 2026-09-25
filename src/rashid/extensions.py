"""Extension pass: validate each object against the schemas it declares.

The structural pass proves an object is valid STAC; the schema pass proves it
satisfies the Portolan profile. Neither opens the schemas of the *other* STAC
extensions the object declares, so an object can declare Projection, set
``proj:code`` to an integer, and pass both. Declaring an extension is a claim
about the object's shape, and this pass checks the claim.

Which extensions Portolan approves, and which version of each it pins, is the
profile's extension registry — vendored to ``_schemas/extension-registry.json``
and read here through :func:`rashid.rules.conformance.registered_extension`.
The same file drives ``scripts/vendor_stac_schemas.py``, so a version pinned in
the spec arrives with its schema on the next spec sync and this module needs no
edit to validate against it.

A registered extension declared at a version the registry does not pin is
validated against the schema of the version it declares. That schema is not in
the wheel, so the pass fetches it over https. Validating it against the pinned
schema would invent errors, and skipping it would let a catalog turn its
schema errors into a PTL-CNF-004 warning by declaring an older version.

An extension the registry does not list is *not* an error. It is reported once
as ``PTL-EXT-002`` so the gap is visible rather than silent, and validated only
when the caller passes ``allow_network=True``.

A schema that cannot be fetched or resolved affects only the objects that
declare it. It is reported once as a ``PTL-EXT-000`` warning, and the pass
goes on to validate every other declared extension.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rashid._jsonschema import (
    SchemaError,
    describe,
    extension_registry,
    fetch_schema,
    vendored_extension_schemas,
)
from rashid.catalog import CatalogGraph, Kind, Node
from rashid.model import Finding, Severity
from rashid.rules.conformance import (
    SCHEMA_URI_PATTERN,
    registered_at_other_version,
    registered_extension,
)

EXT_INVALID = "PTL-EXT-001"
EXT_UNAVAILABLE = "PTL-EXT-000"
EXT_UNVALIDATED = "PTL-EXT-002"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
#
# Empty by design. The spec sentence this pass implements — the structural
# validation bullet in specs/portolan/core.md — carries no RFC 2119 keyword,
# so the manifest assigns it no ID. PTL-CNF-004 sets the same precedent.
SPEC_IDS: dict[str, tuple[str, ...]] = {}

# One object's raw JSON and one schema URI in, that schema's errors out.
Validator = Callable[[dict[str, Any], str], list[SchemaError]]

_EXTENSION_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")

_fetch_schema = fetch_schema


class SchemaUnavailable(Exception):
    """One declared schema could not be fetched or resolved.

    It affects only the objects that declare that URI. Every other exception a
    validator raises is systemic and stops the pass.
    """


def _compile(schema: dict[str, Any]) -> Any:
    """Compile one extension schema against the vendored closure.

    The dialect comes from the schema's own ``$schema`` rather than a
    hardcoded draft. Every registered extension is draft-07 today, but the
    roots are spec-driven and a future one need not be. Hardcoding the dialect
    is the bug stac-check #159 reports upstream; there is no reason to repeat
    it here.
    """
    from jsonschema.validators import validator_for

    return validator_for(schema)(schema, registry=extension_registry())


def default_validator(*, allow_network: bool = False) -> Validator:
    """Build a validator over the vendored extension schemas.

    A URI missing from the wheel is fetched when ``allow_network`` is set, or
    when it is a registered extension at a version the registry does not pin.
    Anything else raises ``LookupError``. A failed fetch or an unresolvable
    ``$ref`` raises :class:`SchemaUnavailable`, and is remembered so the pass
    does not wait on the same dead URI once per object.

    ``jsonschema`` is imported lazily by :func:`_compile`: the metadata pass
    never needs it. Compiled validators are memoized per URI — a catalog
    declares the same handful of extensions on every object, and compiling
    each once per object dominates the pass otherwise.
    """
    from referencing.exceptions import Unresolvable

    store = vendored_extension_schemas()
    compiled: dict[str, Any] = {}
    failed: dict[str, str] = {}

    def _validate(data: dict[str, Any], schema_uri: str) -> list[SchemaError]:
        if schema_uri in failed:
            raise SchemaUnavailable(failed[schema_uri])
        validator = compiled.get(schema_uri)
        if validator is None:
            schema = store.get(schema_uri)
            if schema is None:
                if not allow_network and registered_at_other_version(schema_uri) is None:
                    raise LookupError(f"no vendored schema for {schema_uri}")
                try:
                    schema = _fetch_schema(schema_uri)
                except (OSError, ValueError) as exc:
                    failed[schema_uri] = str(exc)
                    raise SchemaUnavailable(str(exc)) from exc
            validator = compiled[schema_uri] = _compile(schema)
        try:
            errors = sorted(validator.iter_errors(data), key=str)
        except Unresolvable as exc:
            failed[schema_uri] = f"unresolvable $ref: {exc}"
            raise SchemaUnavailable(failed[schema_uri]) from exc
        return [describe(error) for error in errors]

    return _validate


def _declared(node: Node) -> list[str]:
    """The extension URIs an object declares, minus the Portolan profile URI.

    The profile URI is owned by PTL-CNF-001, PTL-CNF-002 and the --schema
    pass. Reporting it here too would double every profile finding.
    """
    declared = node.data.get("stac_extensions")
    if not isinstance(declared, list):
        return []
    return [uri for uri in declared if isinstance(uri, str) and not SCHEMA_URI_PATTERN.match(uri)]


def validate_extensions(
    graph: CatalogGraph,
    validator: Validator | None = None,
    *,
    allow_network: bool = False,
) -> list[Finding]:
    """Validate every object against the extension schemas it declares.

    Returns ``PTL-EXT-001`` errors per violation, each carrying the JSON
    pointer of the offending value. An extension the registry does not list
    yields one ``PTL-EXT-002`` info for the whole catalog, not one per object:
    a catalog that declares an unregistered extension declares it everywhere,
    and hundreds of identical notices would bury the errors. A schema that
    cannot be fetched or resolved yields one ``PTL-EXT-000`` warning per URI,
    and the pass goes on. If the validator cannot be built at all, or fails in
    a way that is not specific to one URI, returns a single ``PTL-EXT-000``
    warning so the offline metadata findings still surface.
    """
    if validator is None:
        try:
            validator = default_validator(allow_network=allow_network)
        except ImportError:
            return [
                Finding(
                    rule_id=EXT_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=(
                        "extension validation skipped: the 'jsonschema' package is not installed"
                    ),
                    path=".",
                )
            ]

    findings: list[Finding] = []
    unvalidated: dict[str, int] = {}
    unavailable: dict[str, tuple[str, str, int]] = {}
    for node in graph.iter(*_EXTENSION_KINDS):
        if node.parse_error is not None:
            continue
        for uri in _declared(node):
            entry = registered_extension(uri) or registered_at_other_version(uri)
            if entry is None and not allow_network:
                unvalidated[uri] = unvalidated.get(uri, 0) + 1
                continue
            label = f"{entry[0]} {entry[1]}" if entry is not None else uri
            try:
                errors = validator(node.data, uri)
            except LookupError:
                unvalidated[uri] = unvalidated.get(uri, 0) + 1
                continue
            except SchemaUnavailable as exc:
                _, _, count = unavailable.get(uri, (label, "", 0))
                unavailable[uri] = (label, str(exc), count + 1)
                continue
            except Exception as exc:  # noqa: BLE001 - any validator failure is systemic
                findings.append(
                    Finding(
                        rule_id=EXT_UNAVAILABLE,
                        severity=Severity.WARNING,
                        message=f"extension validation could not run: {exc}",
                        path=".",
                    )
                )
                return findings
            findings.extend(
                Finding(
                    rule_id=EXT_INVALID,
                    severity=Severity.ERROR,
                    message=f"{label} schema validation failed: {error.message}",
                    path=str(node.path),
                    object_id=node.id,
                    json_pointer=error.json_pointer,
                )
                for error in errors
            )

    findings.extend(
        Finding(
            rule_id=EXT_UNAVAILABLE,
            severity=Severity.WARNING,
            message=(
                f"the {label} schema could not be loaded ({reason}); "
                f"{count} object(s) declaring it were not validated against it"
            ),
            path=".",
            actual=uri,
        )
        for uri, (label, reason, count) in sorted(unavailable.items())
    )
    findings.extend(
        Finding(
            rule_id=EXT_UNVALIDATED,
            severity=Severity.INFO,
            message=(
                f"{uri} is not pinned by the Portolan extension registry; "
                f"{count} object(s) declaring it were not validated against it"
            ),
            path=".",
            fix_hint=(
                "pass --schema-allow-network to fetch and validate this schema, "
                "or propose the extension for the profile's registry"
            ),
            actual=uri,
        )
        for uri, count in sorted(unvalidated.items())
    )
    return findings
