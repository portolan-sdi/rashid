from __future__ import annotations

import pytest

from rashid.model import Finding, Report, Severity

pytestmark = pytest.mark.unit


def _finding(
    severity: Severity, rule_id: str = "PTL-TST-001", path: str = "catalog.json"
) -> Finding:
    return Finding(rule_id=rule_id, severity=severity, message="msg", path=path)


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


def test_to_dict_omits_unset_structured_fields() -> None:
    payload = _finding(Severity.ERROR).to_dict()
    for key in ("expected", "actual", "data"):
        assert key not in payload


def test_to_dict_round_trips_structured_fields() -> None:
    finding = Finding(
        rule_id="PTL-TST-001",
        severity=Severity.ERROR,
        message="file:size is wrong",
        path="roads/collection.json",
        json_pointer="/assets/data/file:size",
        expected=2048,
        actual=1024,
        data={"asset_key": "data"},
    )
    payload = finding.to_dict()
    assert payload["expected"] == 2048
    assert payload["actual"] == 1024
    assert payload["data"] == {"asset_key": "data"}
    import json

    json.dumps(payload)  # the contract: every field JSON-serializable


def test_by_rule_counts_findings_and_distinct_files() -> None:
    report = Report(
        findings=[
            _finding(Severity.ERROR, "PTL-AST-003", "a.json"),
            _finding(Severity.ERROR, "PTL-AST-003", "a.json"),
            _finding(Severity.ERROR, "PTL-AST-003", "b.json"),
        ]
    )
    (summary,) = report.by_rule()
    assert summary.rule_id == "PTL-AST-003"
    assert summary.count == 3
    assert summary.file_count == 2


def test_by_rule_splits_a_rule_that_emits_at_two_severities() -> None:
    report = Report(
        findings=[
            _finding(Severity.ERROR, "PTL-DAT-007"),
            _finding(Severity.WARNING, "PTL-DAT-007"),
        ]
    )
    error, warning = report.by_rule()
    assert (error.severity, error.count) == (Severity.ERROR, 1)
    assert (warning.severity, warning.count) == (Severity.WARNING, 1)


def test_by_rule_sorts_loudest_first() -> None:
    report = Report(
        findings=[
            _finding(Severity.INFO, "PTL-PRO-002"),
            _finding(Severity.WARNING, "PTL-TTL-002"),
            _finding(Severity.ERROR, "PTL-AST-002"),
            *[_finding(Severity.ERROR, "PTL-AST-003") for _ in range(4)],
        ]
    )
    assert [s.rule_id for s in report.by_rule()] == [
        "PTL-AST-003",  # error, 4 findings
        "PTL-AST-002",  # error, 1 finding
        "PTL-TTL-002",  # warning
        "PTL-PRO-002",  # info
    ]


def test_by_rule_carries_the_registry_description() -> None:
    from rashid.registry import describe

    report = Report(findings=[_finding(Severity.ERROR, "PTL-AST-003")])
    assert report.by_rule()[0].description == describe("PTL-AST-003")


def test_by_rule_leaves_an_unknown_rule_id_undescribed() -> None:
    report = Report(findings=[_finding(Severity.ERROR, "PTL-ZZZ-999")])
    assert report.by_rule()[0].description == ""


def test_by_rule_is_empty_for_a_clean_report() -> None:
    assert Report().by_rule() == []


def test_report_to_dict_carries_a_summary_block() -> None:
    report = Report(
        findings=[
            _finding(Severity.ERROR, "PTL-AST-003", "a.json"),
            _finding(Severity.ERROR, "PTL-AST-003", "b.json"),
            _finding(Severity.WARNING, "PTL-TTL-002", "a.json"),
        ],
        files_checked=2,
    )
    summary = report.to_dict()["summary"]
    assert summary["by_severity"] == {"error": 2, "warning": 1, "info": 0}
    assert summary["by_rule"][0] == {
        "rule_id": "PTL-AST-003",
        "severity": "error",
        "count": 2,
        "file_count": 2,
        "description": "every asset should carry file:size and file:checksum",
    }


def test_report_to_dict_still_lists_every_finding() -> None:
    """The summary is additive: machine consumers of findings keep working."""
    report = Report(findings=[_finding(Severity.ERROR) for _ in range(5)])
    payload = report.to_dict()
    assert len(payload["findings"]) == 5
    assert payload["error_count"] == 5
