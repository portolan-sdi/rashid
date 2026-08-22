"""Runner configuration and CLI behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from rashid import RulesConfig, Severity, validate
from rashid.cli import main
from tests.conftest import CatalogBuilder, findings_for, rule_ids

pytestmark = pytest.mark.integration


def test_disable_rule(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", title="road_centerlines")
    root = catalog.write()
    assert "PTL-TTL-002" in rule_ids(validate(root))
    config = RulesConfig(disabled=frozenset({"PTL-TTL-002"}))
    assert "PTL-TTL-002" not in rule_ids(validate(root, config=config))


def test_severity_override_promotes_warning_to_error(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", title="road_centerlines")
    root = catalog.write()
    assert validate(root).passed  # TTL-002 is a warning by default
    config = RulesConfig(severity_overrides={"PTL-TTL-002": Severity.ERROR})
    report = validate(root, config=config)
    assert not report.passed
    assert findings_for(report, "PTL-TTL-002")[0].severity is Severity.ERROR


def test_config_from_dict(catalog: CatalogBuilder) -> None:
    config = RulesConfig.from_dict(
        {"disabled": ["PTL-TTL-002"], "severity": {"PTL-PRO-002": "warning"}}
    )
    assert "PTL-TTL-002" in config.disabled
    assert config.severity_overrides["PTL-PRO-002"] is Severity.WARNING


def test_cli_clean_catalog_exits_zero(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    result = CliRunner().invoke(main, ["check", "--no-structural", str(root)])
    assert result.exit_code == 0
    assert "no findings" in result.output


def test_cli_broken_catalog_exits_one(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="proprietary")
    root = catalog.write()
    result = CliRunner().invoke(main, ["check", str(root)])
    assert result.exit_code == 1
    assert "PTL-LIC-003" in result.output


def test_cli_json_output(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="proprietary")
    root = catalog.write()
    result = CliRunner().invoke(main, ["check", "--no-structural", "--json", str(root)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["passed"] is False
    assert payload["error_count"] == 1
    assert payload["findings"][0]["rule_id"] == "PTL-LIC-003"


def test_cli_missing_path_is_usage_error() -> None:
    result = CliRunner().invoke(main, ["check", "/definitely/not/here"])
    assert result.exit_code == 2


def test_validate_accepts_the_root_catalog_json(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="proprietary")
    root = catalog.write()
    assert rule_ids(validate(root / "catalog.json")) == rule_ids(validate(root))


def test_cli_accepts_the_root_catalog_json(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="proprietary")
    root = catalog.write()
    from_dir = CliRunner().invoke(main, ["check", "--no-structural", "--json", str(root)])
    from_file = CliRunner().invoke(
        main, ["check", "--no-structural", "--json", str(root / "catalog.json")]
    )
    assert from_file.exit_code == from_dir.exit_code == 1
    assert json.loads(from_file.output) == json.loads(from_dir.output)


def test_cli_rejects_a_file_that_is_not_the_root_catalog(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    result = CliRunner().invoke(main, ["check", str(root / "roads" / "collection.json")])
    assert result.exit_code == 1
    assert "PTL-GEN-000" in result.output
    assert "catalog.json" in result.output


def _noisy(catalog: CatalogBuilder, count: int) -> Path:
    """A catalog whose every collection trips one warning: ``count`` findings.

    From twenty collections up, the root catalog also trips ``PTL-CAT-001``,
    the subcatalog fan-out warning, for one finding more.
    """
    for index in range(count):
        catalog.collection(f"roads_{index}", title=f"road_centerlines_{index}")
    return catalog.write()


def test_cli_lists_findings_below_the_threshold(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 3)
    result = CliRunner().invoke(main, ["check", str(root)])
    assert "Too many to list" not in result.output
    assert result.output.count("PTL-TTL-002") == 3
    assert "3 error(s)" not in result.output  # they are warnings
    assert "0 error(s), 3 warning(s), 0 info(s)" in result.output


def test_cli_summarizes_above_the_threshold(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 60)
    result = CliRunner().invoke(main, ["check", str(root)])
    assert "Too many to list" in result.output
    # One rule block, not sixty lines: the id appears once, with its count.
    assert result.output.count("PTL-TTL-002") == 1
    assert "60x" in result.output
    assert "... 57 more in 60 files" in result.output
    assert "titles must be human-readable" in result.output


def test_cli_summary_keeps_the_counts_line(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 60)
    result = CliRunner().invoke(main, ["check", str(root)])
    # Sixty title warnings, plus the root's own fan-out warning.
    assert "0 error(s), 61 warning(s), 0 info(s) across" in result.output
    assert result.exit_code == 0  # warnings alone still pass


def test_cli_all_lists_every_finding_above_the_threshold(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 60)
    result = CliRunner().invoke(main, ["check", str(root), "--all"])
    assert "Too many to list" not in result.output
    assert result.output.count("PTL-TTL-002") == 60


def test_cli_summary_collapses_below_the_threshold(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 3)
    result = CliRunner().invoke(main, ["check", str(root), "--summary"])
    assert result.output.count("PTL-TTL-002") == 1
    assert "3x" in result.output
    # The note explains an automatic collapse; here the caller asked for it.
    assert "Too many to list" not in result.output


def test_cli_summary_shows_examples_without_a_more_line(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 2)
    result = CliRunner().invoke(main, ["check", str(root), "--summary"])
    assert result.output.count("collection.json:") == 2
    assert "more in" not in result.output


def test_cli_all_and_summary_conflict(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 3)
    result = CliRunner().invoke(main, ["check", str(root), "--all", "--summary"])
    assert result.exit_code == 2
    assert "opposite things" in result.output


def test_cli_summary_mode_keeps_the_failing_exit_code(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", license="proprietary")
    root = _noisy(catalog, 60)
    result = CliRunner().invoke(main, ["check", str(root)])
    assert "Too many to list" in result.output
    assert result.exit_code == 1


def test_cli_json_carries_the_summary_and_every_finding(catalog: CatalogBuilder) -> None:
    root = _noisy(catalog, 60)
    payload = json.loads(CliRunner().invoke(main, ["check", str(root), "--json"]).output)
    assert len(payload["findings"]) == 61  # JSON never truncates
    assert payload["summary"]["by_severity"] == {"error": 0, "warning": 61, "info": 0}
    rows = {row["rule_id"]: row for row in payload["summary"]["by_rule"]}
    assert rows["PTL-TTL-002"]["count"] == 60
    assert rows["PTL-TTL-002"]["file_count"] == 60
    # The root's flat sixty children trip the fan-out warning once.
    assert rows["PTL-CAT-001"]["count"] == 1
