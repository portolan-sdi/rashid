"""The --json report matches its published JSON Schema (issue #207)."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from rashid_core import ValidationOptions, _profile, validate
from rashid_core.cli import main
from rashid_core.report import REPORT_SCHEMA_URI, REPORT_VERSION
from tests.conftest import PORTOLAN_URI, CatalogBuilder, mutate_json

pytestmark = pytest.mark.unit


def _schema() -> dict[str, Any]:
    path = resources.files("rashid_core").joinpath(f"_schemas/report/v{REPORT_VERSION}/schema.json")
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def _cli_report(args: list[str]) -> dict[str, Any]:
    result = CliRunner().invoke(main, args)
    assert result.exit_code in (0, 1), result.output
    report: dict[str, Any] = json.loads(result.stdout)
    return report


def test_the_schema_is_valid_and_names_the_uri_the_report_cites() -> None:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$id"] == REPORT_SCHEMA_URI
    assert f"/v{REPORT_VERSION}/" in REPORT_SCHEMA_URI


@pytest.mark.parametrize("timings", [False, True])
def test_a_clean_report_matches_the_schema(catalog: CatalogBuilder, timings: bool) -> None:
    catalog.collection("roads")
    args = [*(["--timings"] if timings else []), "check", "--json", str(catalog.write())]
    report = _cli_report(args)
    Draft202012Validator(_schema()).validate(report)
    assert report["$schema"] == REPORT_SCHEMA_URI
    assert ("meta" in report) is timings


def test_a_report_with_findings_matches_the_schema(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("license"))
    report = _cli_report(["check", "--json", "--schema", str(root)])
    assert report["findings"]
    Draft202012Validator(_schema()).validate(report)


def test_the_schema_rejects_a_report_without_the_validator_block(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    report = _cli_report(["check", "--json", str(catalog.write())])
    del report["validator"]
    assert list(Draft202012Validator(_schema()).iter_errors(report))


def test_the_validator_block_names_the_versions_and_the_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    options = ValidationOptions(structural=False, extensions=False, data=False, schema=True)
    block = validate(catalog.write(), options=options).to_dict()["validator"]
    assert block["declared_profile"] == PORTOLAN_URI
    assert block["applied_profile"] == _profile.newest_profile_version()
    assert block["bundled_profiles"] == list(_profile.bundled_profile_versions())
    assert block["spec_commit"] == _profile.spec_commit()
    assert block["passes"] == ["metadata", "schema"]


def test_a_root_with_two_profile_uris_declares_none(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    second = _profile.profile_uri(_profile.bundled_profile_versions()[0])
    mutate_json(root / "catalog.json", lambda d: d["stac_extensions"].append(second))
    assert validate(root).to_dict()["validator"]["declared_profile"] is None
