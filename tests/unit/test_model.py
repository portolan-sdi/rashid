from __future__ import annotations

import pytest

from rashid.model import Finding, Report, Severity

pytestmark = pytest.mark.unit


def _finding(severity: Severity, rule_id: str = "PTL-TST-001") -> Finding:
    return Finding(rule_id=rule_id, severity=severity, message="msg", path="catalog.json")


def test_report_passes_when_empty() -> None:
    assert Report().passed


def test_report_passes_with_warnings_and_infos_only() -> None:
    report = Report(findings=[_finding(Severity.WARNING), _finding(Severity.INFO)])
    assert report.passed
    assert report.errors == []
    assert len(report.warnings) == 1
    assert len(report.infos) == 1


def test_report_fails_on_any_error() -> None:
    report = Report(findings=[_finding(Severity.WARNING), _finding(Severity.ERROR)])
    assert not report.passed
    assert len(report.errors) == 1


def test_finding_to_dict_omits_empty_optionals() -> None:
    d = _finding(Severity.ERROR).to_dict()
    assert d == {
        "rule_id": "PTL-TST-001",
        "severity": "error",
        "message": "msg",
        "path": "catalog.json",
    }


def test_finding_to_dict_includes_optionals() -> None:
    finding = Finding(
        rule_id="PTL-TST-001",
        severity=Severity.INFO,
        message="msg",
        path="c/collection.json",
        object_id="c",
        json_pointer="/links/0",
        fix_hint="do the thing",
    )
    d = finding.to_dict()
    assert d["object_id"] == "c"
    assert d["json_pointer"] == "/links/0"
    assert d["fix_hint"] == "do the thing"


def test_report_to_dict_counts() -> None:
    report = Report(
        findings=[_finding(Severity.ERROR), _finding(Severity.WARNING)], files_checked=3
    )
    d = report.to_dict()
    assert d["passed"] is False
    assert d["files_checked"] == 3
    assert d["error_count"] == 1
    assert d["warning_count"] == 1
    assert d["info_count"] == 0
    assert len(d["findings"]) == 2
