"""Runner configuration and CLI behavior."""

from __future__ import annotations

import json

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
