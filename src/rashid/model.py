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
        expected: The correct value at that location per the authoritative
            source (e.g. the recomputed checksum, the served byte count), when
            the rule compares two concrete values. JSON-serializable.
        actual: What the object currently declares there. JSON-serializable.
        data: Optional rule-specific machine-usable context for automated
            remediation. JSON-serializable. ``expected``/``actual``/``data``
            make findings actionable without parsing prose; note they also make
            a Finding unhashable in practice when they hold mutable values.
    """

    rule_id: str
    severity: Severity
    message: str
    path: str
    object_id: str | None = None
    json_pointer: str | None = None
    fix_hint: str | None = None
    expected: Any | None = None
    actual: Any | None = None
    data: dict[str, Any] | None = None

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
        if self.expected is not None:
            d["expected"] = self.expected
        if self.actual is not None:
            d["actual"] = self.actual
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass(frozen=True)
class RuleSummary:
    """How often one check fired, and over how much of the catalog.

    A catalog that violates a per-asset rule produces one finding per asset,
    so a large catalog can yield thousands of near-identical lines. Collapsing
    them to a count plus the rule's description is what makes such a report
    readable.

    Attributes:
        rule_id: Stable identifier of the check, e.g. ``PTL-AST-003``.
        severity: Severity of the findings counted here. A check that emits at
            two severities yields two summaries, one per severity.
        count: Number of findings with this rule id and severity.
        file_count: Number of distinct files those findings name.
        description: One-line statement of what the check requires; empty when
            the rule id is not in the registry.
    """

    rule_id: str
    severity: Severity
    count: int
    file_count: int
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "count": self.count,
            "file_count": self.file_count,
            "description": self.description,
        }


# Loudest first: errors before warnings before infos.
_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


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

    def by_rule(self) -> list[RuleSummary]:
        """Findings collapsed to one row per (rule id, severity), loudest first.

        Rows sort by severity, then by count descending, then by rule id, so
        the check responsible for the most findings leads the report.
        """
        from rashid.registry import describe

        counts: dict[tuple[str, Severity], int] = {}
        paths: dict[tuple[str, Severity], set[str]] = {}
        for finding in self.findings:
            key = (finding.rule_id, finding.severity)
            counts[key] = counts.get(key, 0) + 1
            paths.setdefault(key, set()).add(finding.path)

        summaries = [
            RuleSummary(
                rule_id=rule_id,
                severity=severity,
                count=count,
                file_count=len(paths[(rule_id, severity)]),
                description=describe(rule_id),
            )
            for (rule_id, severity), count in counts.items()
        ]
        summaries.sort(key=lambda s: (_SEVERITY_RANK[s.severity], -s.count, s.rule_id))
        return summaries

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict for machine output."""
        return {
            "passed": self.passed,
            "files_checked": self.files_checked,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.infos),
            "summary": {
                "by_severity": {
                    "error": len(self.errors),
                    "warning": len(self.warnings),
                    "info": len(self.infos),
                },
                "by_rule": [s.to_dict() for s in self.by_rule()],
            },
            "findings": [f.to_dict() for f in self.findings],
        }
