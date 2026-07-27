"""Validation finding and report data structures.

A rule emits zero or more findings; the absence of findings is a pass.
A report aggregates every finding produced by one validation run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(Enum):
    """Severity of a finding.

    ERROR: a MUST requirement is violated; the catalog does not conform.
    WARNING: a SHOULD requirement is violated, or a MUST with an explicit
        spec exception (e.g. schema-URI mismatch from the root).
    INFO: a suggestion the validator cannot fully decide from metadata.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    """A single defect found in a single object.

    Attributes:
        rule_id: Stable identifier of the rule, e.g. ``PTL-LNK-006``.
        severity: Severity of this finding.
        message: Human-readable description of the defect.
        path: POSIX path of the offending file, relative to the catalog root.
        object_id: STAC ``id`` of the offending object, when known.
        json_pointer: Optional locator within the file, e.g. ``/links/3/href``.
        fix_hint: Optional suggestion for fixing the issue.
    """

    rule_id: str
    severity: Severity
    message: str
    path: str
    object_id: str | None = None
    json_pointer: str | None = None
    fix_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict, omitting empty optionals."""
        d: dict[str, Any] = {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
        }
        if self.object_id is not None:
            d["object_id"] = self.object_id
        if self.json_pointer is not None:
            d["json_pointer"] = self.json_pointer
        if self.fix_hint is not None:
            d["fix_hint"] = self.fix_hint
        return d


@dataclass
class Report:
    """Aggregate of all findings from one validation run."""

    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0

    @property
    def passed(self) -> bool:
        """True when no ERROR-severity finding was produced."""
        return not any(f.severity is Severity.ERROR for f in self.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.INFO]

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict for machine output."""
        return {
            "passed": self.passed,
            "files_checked": self.files_checked,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.infos),
            "findings": [f.to_dict() for f in self.findings],
        }
