"""Structural pass: STAC 1.1.0 core validation, delegated to stac-validator.

The metadata pass (``rashid.rules``) checks Portolan requirements over raw JSON
and is deliberately stdlib-only. STAC *structural* validity — that each object
satisfies the STAC 1.1.0 core schema for its type — is a separable pass the
Portolan spec explicitly delegates to a STAC validator (profile: Validation,
pass 1). This module runs that pass by calling ``stac-validator`` (published as
the maintained ``stac-valid`` distribution, imported as ``stac_validator``) over
every object in the graph.

The validator is injectable so the pass can be exercised offline in tests; the
default implementation reaches ``schemas.stacspec.org`` to fetch core schemas
(results are cached in-process). A validator that cannot run — the package is
absent, or the schemas are unreachable — downgrades to a single WARNING so the
offline metadata findings still surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rashid.catalog import CatalogGraph, Kind
from rashid.model import Finding, Severity

STR_INVALID = "PTL-STR-001"
STR_UNAVAILABLE = "PTL-STR-000"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
SPEC_IDS: dict[str, tuple[str, ...]] = {
    # STAC 1.1.0 validity of the entrypoint and every object, including
    # the Item MUSTs (id, geometry/bbox, datetime, assets, links) and the
    # STAC-required collection extent that a tabular collection still
    # carries (PORTO-FMT-036).
    STR_INVALID: (
        "PORTO-CORE-001",
        "PORTO-CORE-013",
        "PORTO-CORE-019",
        "PORTO-FMT-036",
    ),
}

# A validator maps one object's raw JSON to a list of structural error messages;
# an empty list means the object is structurally valid STAC.
Validator = Callable[[dict[str, Any]], list[str]]

_STRUCTURAL_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")


def default_validator() -> Validator:
    """Build the stac-validator-backed structural validator.

    Imported lazily: the metadata pass never needs ``stac_validator``, and the
    import both pulls a third-party dependency and, on first use, fetches STAC
    core schemas over the network.
    """
    from stac_validator import stac_validator

    def _validate(data: dict[str, Any]) -> list[str]:
        # core=True validates against the STAC 1.1.0 core schema only. The
        # extension schemas an object declares — including the Portolan profile
        # itself — are the metadata pass's domain (rashid implements the profile's
        # requirements by hand), so the structural pass must not try to fetch
        # and apply them.
        validator = stac_validator.StacValidate(core=True)
        validator.validate_dict(data)
        errors: list[str] = []
        for message in validator.message:
            if message.get("valid_stac"):
                continue
            detail = (
                message.get("error_message")
                or message.get("error_type")
                or "object is not structurally valid STAC"
            )
            schema = message.get("failed_schema")
            errors.append(f"{detail} (schema: {schema})" if schema else str(detail))
        return errors

    return _validate


def validate_structural(graph: CatalogGraph, validator: Validator | None = None) -> list[Finding]:
    """Validate every catalog, collection, and item against STAC 1.1.0 core.

    Returns ``PTL-STR-001`` errors for each structural failure. If the validator
    is unavailable (missing package) or a call fails (typically the schema fetch
    could not reach the network), returns a single ``PTL-STR-000`` warning
    instead of failing the run — a systemic failure is reported once, not once
    per object.
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
                        "structural validation skipped: the 'stac-valid' package is not installed"
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
                    message=f"STAC 1.1.0 structural validation failed: {error}",
                    path=str(node.path),
                    object_id=node.id,
                )
            )
    return findings
