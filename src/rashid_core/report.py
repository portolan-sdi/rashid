"""Aggregated validation results and their JSON representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rashid_core import _package, _profile
from rashid_core.model import Finding, Severity
from rashid_core.registry import describe


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


# The JSON Schema of Report.to_dict(), shipped at _schemas/report/<version>/.
REPORT_VERSION = "1.0.0"
REPORT_SCHEMA_URI = f"https://schemas.portolan-sdi.org/rashid/report/v{REPORT_VERSION}/schema.json"

# Loudest first: errors before warnings before infos.
_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass
class Report:
    """Aggregate of all findings from one validation run.

    ``declared_profile`` is the Portolan schema URI the root catalog declares,
    when it declares exactly one. ``passes`` names the passes that ran.
    """

    findings: list[Finding] = field(default_factory=list)
    files_checked: int = 0
    declared_profile: str | None = None
    passes: tuple[str, ...] = ()

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
        """Convert to a JSON-serializable dict for machine output.

        The shape follows the JSON Schema at ``REPORT_SCHEMA_URI``.
        """
        return {
            "$schema": REPORT_SCHEMA_URI,
            "report_version": REPORT_VERSION,
            "validator": {
                "name": "rashid",
                "version": _package.version(),
                "spec_commit": _profile.spec_commit(),
                "bundled_profiles": list(_profile.bundled_profile_versions()),
                "applied_profile": _profile.newest_profile_version(),
                "declared_profile": self.declared_profile,
                "passes": list(self.passes),
            },
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
