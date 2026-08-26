"""The single enumeration of every check rashid can emit.

A finding carries a rule id and nothing else identifying the check behind it.
Anything that reports on findings in aggregate — the CLI's per-rule summary,
the JSON ``summary`` block, the spec-coverage gate — needs to turn that id back
into a title and a severity. This module owns that join.

Two id-spaces feed it. Metadata rules carry their own metadata as class
variables and are enumerated by :data:`rashid.rules.DEFAULT_RULES`. The
structural, schema, data, live, and runner passes predate the ``Rule``
abstraction and emit findings directly; they declare only the spec IDs they
enforce, in a module-level ``SPEC_IDS`` dict, so their titles and severities
live in :data:`_PASS_CHECKS` below.

Building the join enforces that the two halves stay honest: the id-spaces must
stay disjoint, and every ``SPEC_IDS`` key must have an entry here. A new pass
check that forgets its title raises :class:`RegistryError` at import rather
than rendering a blank column.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

import rashid.data as _data_pass
import rashid.extensions as _extension_pass
import rashid.live as _live_pass
import rashid.runner as _runner
import rashid.schema as _schema_pass
import rashid.structural as _structural_pass
from rashid.model import Severity
from rashid.rules import DEFAULT_RULES

__all__ = ["CHECKS", "CheckInfo", "RegistryError", "describe"]


@dataclass(frozen=True)
class CheckInfo:
    """What a rule id means, independent of any one finding.

    Attributes:
        id: Stable check identifier, e.g. ``PTL-AST-003``.
        description: One-line statement of what the check requires, phrased as
            the passing condition.
        severity: The strongest severity the check can emit. Most checks emit
            exactly one; ``PTL-DAT-007`` and ``PTL-DAT-010`` emit a MUST
            failure at ERROR and a companion SHOULD at WARNING, and
            ``PTL-VIZ-001`` softens to WARNING when it cannot decide whether
            its MUST applies. Each records the strongest it reaches.
        spec_ids: Requirement IDs from the spec's manifest that this check
            enforces. Empty for the checks that report a pass could not run,
            which discharge no requirement of their own.
    """

    id: str
    description: str
    severity: Severity
    spec_ids: tuple[str, ...] = ()


# Title and strongest severity for every check that does not subclass Rule.
# Keys are the rule ids themselves rather than the modules' constants so this
# table reads as the id-space it documents. The four ``-000`` entries report
# that a pass could not run: they cite no requirement and always warn, because
# an unavailable pass is not a failed one.
_PASS_CHECKS: dict[str, tuple[Severity, str]] = {
    "PTL-GEN-000": (
        Severity.ERROR,
        "the catalog root has a readable catalog.json",
    ),
    "PTL-GEN-001": (
        Severity.ERROR,
        "every object file parses as JSON",
    ),
    "PTL-STR-000": (
        Severity.WARNING,
        "the STAC structural pass could run",
    ),
    "PTL-STR-001": (
        Severity.ERROR,
        "every object is valid STAC 1.1.0",
    ),
    "PTL-EXT-000": (
        Severity.WARNING,
        "the extension schema pass could run",
    ),
    "PTL-EXT-001": (
        Severity.ERROR,
        "every object satisfies the schemas of the extensions it declares",
    ),
    "PTL-EXT-002": (
        Severity.INFO,
        "every declared extension was validated against its schema",
    ),
    "PTL-SCH-000": (
        Severity.WARNING,
        "the Portolan profile schema pass could run",
    ),
    "PTL-SCH-001": (
        Severity.ERROR,
        "every object matches the Portolan profile schema it declares",
    ),
    "PTL-DAT-000": (
        Severity.WARNING,
        "the data pass could run",
    ),
    "PTL-DAT-001": (
        Severity.ERROR,
        "asset bytes hash to the declared file:checksum",
    ),
    "PTL-DAT-002": (
        Severity.ERROR,
        "asset byte count matches the declared file:size",
    ),
    "PTL-DAT-003": (
        Severity.ERROR,
        "asset bytes are in the format the declared media type names",
    ),
    "PTL-DAT-004": (
        Severity.ERROR,
        "every raster asset is a valid cloud-optimized GeoTIFF",
    ),
    "PTL-DAT-005": (
        Severity.WARNING,
        "asset footprint and CRS agree with the declaring metadata",
    ),
    "PTL-DAT-006": (
        Severity.ERROR,
        "GeoParquet rows are spatially ordered",
    ),
    "PTL-DAT-007": (
        Severity.ERROR,
        "GeoParquet row groups carry spatial statistics",
    ),
    "PTL-DAT-008": (
        Severity.ERROR,
        "GeoParquet row groups stay within the size ceiling",
    ),
    "PTL-DAT-009": (
        Severity.ERROR,
        "every COG band carries embedded min/max/mean/stddev statistics",
    ),
    "PTL-DAT-010": (
        Severity.ERROR,
        "a COG band with nodata declares its valid percent",
    ),
    "PTL-DAT-011": (
        Severity.ERROR,
        "a raster larger than one internal tile carries internal overviews",
    ),
    "PTL-DAT-012": (
        Severity.ERROR,
        "the geo metadata declares GeoParquet 1.1 or 2.x",
    ),
    "PTL-DAT-013": (
        Severity.ERROR,
        "internal tiles are square and no larger than 512x512",
    ),
    "PTL-DAT-014": (
        Severity.ERROR,
        "every partition file shares a single Parquet schema",
    ),
    "PTL-DAT-015": (
        Severity.WARNING,
        "a plain-Parquet data asset declares table:columns and temporal extent",
    ),
    "PTL-DAT-016": (
        Severity.ERROR,
        "an item mirror reproduces the collection's items",
    ),
    "PTL-DAT-017": (
        Severity.WARNING,
        "a GeoParquet data asset's columns are documented with table:columns",
    ),
    "PTL-LIV-000": (
        Severity.WARNING,
        "the live pass could reach a host to probe",
    ),
    "PTL-LIV-001": (
        Severity.ERROR,
        "the server honors ranged GET with 206 and Accept-Ranges: bytes",
    ),
    "PTL-LIV-002": (
        Severity.ERROR,
        "HEAD returns a Content-Length matching the declared file:size",
    ),
    "PTL-LIV-003": (
        Severity.ERROR,
        "the server allows the requesting CORS origin",
    ),
    "PTL-LIV-004": (
        Severity.ERROR,
        "the server exposes the headers a browser client reads",
    ),
    "PTL-LIV-005": (
        Severity.ERROR,
        "CORS preflight accepts GET and HEAD with Range",
    ),
}

# The pass modules that declare a SPEC_IDS registry.
_PASS_MODULES = (
    _runner,
    _structural_pass,
    _extension_pass,
    _schema_pass,
    _data_pass,
    _live_pass,
)


class RegistryError(RuntimeError):
    """The two id-spaces disagree: a duplicate, a collision, or a missing title.

    Raised while building :data:`CHECKS`, so importing rashid at all fails
    rather than leaving a check that renders without a description.
    """


def _build() -> dict[str, CheckInfo]:
    """Join the two id-spaces into one id -> CheckInfo mapping."""
    checks: dict[str, CheckInfo] = {}
    for rule in DEFAULT_RULES:
        if rule.id in checks:
            raise RegistryError(f"duplicate rule id {rule.id}")
        if not rule.description:
            raise RegistryError(f"rule {rule.id} has no description")
        checks[rule.id] = CheckInfo(
            id=rule.id,
            description=rule.description,
            severity=rule.default_severity,
            spec_ids=rule.spec_ids,
        )

    spec_ids: dict[str, tuple[str, ...]] = {}
    for module in _PASS_MODULES:
        for check_id, ids in module.SPEC_IDS.items():
            if check_id in spec_ids:
                raise RegistryError(f"duplicate check id {check_id}")
            spec_ids[check_id] = ids

    for check_id, (severity, description) in _PASS_CHECKS.items():
        if check_id in checks:
            raise RegistryError(f"pass check {check_id} collides with a rule")
        checks[check_id] = CheckInfo(
            id=check_id,
            description=description,
            severity=severity,
            spec_ids=spec_ids.get(check_id, ()),
        )

    missing = sorted(set(spec_ids) - set(_PASS_CHECKS))
    if missing:
        raise RegistryError(f"pass checks missing from the registry: {missing}")
    return checks


#: Every check rashid can emit, keyed by rule id.
CHECKS: Mapping[str, CheckInfo] = MappingProxyType(_build())


def describe(rule_id: str) -> str:
    """The one-line description of ``rule_id``, or ``""`` when unknown.

    Reporting code renders whatever ids a report carries, including ids from a
    future version or a caller's own rule, so an unknown id yields an empty
    column rather than raising.
    """
    info = CHECKS.get(rule_id)
    return info.description if info is not None else ""
