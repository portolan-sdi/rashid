"""Structural pass: STAC 1.1.0 core validation against the vendored schemas.

The metadata pass (``rashid.rules``) checks Portolan requirements over raw JSON
and is deliberately stdlib-only. STAC *structural* validity — that each object
satisfies the STAC 1.1.0 core schema for its type — is a separable pass the
Portolan spec explicitly delegates to a STAC validator (profile: Validation,
pass 1). This module runs that pass by compiling the STAC core schemas shipped
in the wheel (``rashid/_schemas/stac``, plus the GeoJSON geometry schemas
``item.json`` references) and validating every object against the schema its
``type`` selects. No network is involved.

The validator is injectable so tests can substitute their own. A validator that
cannot run — ``jsonschema`` absent, or a schema failure — downgrades to a
single WARNING so the offline metadata findings still surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rashid._jsonschema import SchemaError, stac_registry
from rashid.catalog import CatalogGraph, Kind
from rashid.model import Finding, Severity

STR_INVALID = "PTL-STR-001"
STR_UNAVAILABLE = "PTL-STR-000"

SPEC_IDS: dict[str, tuple[str, ...]] = {
    STR_INVALID: (
        "PORTO-CORE-001",
        "PORTO-CORE-013",
        "PORTO-CORE-019",
        "PORTO-FMT-036",
    ),
}

Validator = Callable[[dict[str, Any]], list[SchemaError]]

_STRUCTURAL_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")

# STAC discriminates objects by their `type`; each maps to one core root schema.
_ROOT_SCHEMAS: dict[str, str] = {
    "Catalog": "catalog-spec/json-schema/catalog.json",
    "Collection": "collection-spec/json-schema/collection.json",
    "Feature": "item-spec/json-schema/item.json",
}


def default_validator() -> Validator:
    """Build the structural validator over the vendored STAC 1.1.0 closure.

    ``jsonschema`` is imported lazily: the metadata pass never needs it. Each
    object is validated against the core schema its ``type`` selects; an
    unknown or missing ``type`` is itself the structural error.
    """
    from jsonschema import Draft7Validator

    from rashid._jsonschema import _STAC_BASE, STAC_VERSION, describe

    registry = stac_registry()
    validators = {
        stac_type: Draft7Validator({"$ref": f"{_STAC_BASE}{path}"}, registry=registry)
        for stac_type, path in _ROOT_SCHEMAS.items()
    }

    def _validate(data: dict[str, Any]) -> list[SchemaError]:
        stac_type = data.get("type")
        validator = validators.get(stac_type) if isinstance(stac_type, str) else None
        if validator is None:
            expected = ", ".join(sorted(_ROOT_SCHEMAS))
            return [
                SchemaError(
                    message=(
                        f"'type' must be one of {expected} for STAC {STAC_VERSION}, "
                        f"got {stac_type!r}"
                    ),
                    json_pointer="/type",
                )
            ]
        return [describe(error) for error in sorted(validator.iter_errors(data), key=str)]

    return _validate


def validate_structural(graph: CatalogGraph, validator: Validator | None = None) -> list[Finding]:
    """Validate every catalog, collection, and item against STAC 1.1.0 core.

    Returns ``PTL-STR-001`` errors for each structural failure, each carrying
    the JSON pointer of the violation. If the validator is unavailable
    (``jsonschema`` missing) or a call fails, returns a single ``PTL-STR-000``
    warning instead of failing the run — a systemic failure is reported once,
    not once per object.
    """
    if validator is None:
        try:
            validator = default_validator()
        except ImportError:
            return [
                Finding(
                    rule_id=STR_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=(
                        "structural validation skipped: the 'jsonschema' package is not installed"
                    ),
                    path=".",
                )
            ]

    findings: list[Finding] = []
    for node in graph.iter(*_STRUCTURAL_KINDS):
        if node.parse_error is not None:
            continue
        try:
            errors = validator(node.data)
        except Exception as exc:  # noqa: BLE001 - any validator failure is systemic
            findings.append(
                Finding(
                    rule_id=STR_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=f"structural validation could not run: {exc}",
                    path=".",
                )
            )
            return findings
        for error in errors:
            findings.append(
                Finding(
                    rule_id=STR_INVALID,
                    severity=Severity.ERROR,
                    message=f"STAC 1.1.0 structural validation failed: {error.message}",
                    path=str(node.path),
                    object_id=node.id,
                    json_pointer=error.json_pointer,
                )
            )
    return findings
