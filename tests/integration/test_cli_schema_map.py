"""Local drafts override profile schemas through the real CLI and schema engines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from rashid import RulesConfig, ValidationOptions, _trace, validate
from rashid.cli import SchemaMapping, main
from tests._https import HttpsServer, Route
from tests.conftest import PORTOLAN_URI, CatalogBuilder

pytestmark = pytest.mark.integration

DRAFT_URI = "https://schemas.portolan-sdi.org/portolan/v99.0.0/schema.json"
_FLAGS = [
    "check",
    "--schema",
    "--no-data",
    "--no-extensions",
    "--no-structural",
    "--no-live",
    "--json",
]


def _schema_file(tmp_path: Path, name: str, schema: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


@pytest.mark.parametrize("location", ["directory", "file", "https"])
@pytest.mark.parametrize("allow_network", [False, True])
def test_draft_map_validates_every_object_once(
    catalog: CatalogBuilder,
    tmp_path: Path,
    https_server: HttpsServer,
    location: str,
    allow_network: bool,
) -> None:
    catalog.overrides["stac_extensions"] = [DRAFT_URI]
    catalog.collection("roads").item("segment")
    root = catalog.write()
    path = _schema_file(tmp_path, "draft=schema.json", {"required": ["draft_proof"]})
    unused = _schema_file(tmp_path, "other.json", {})
    if location == "https":
        for document in root.rglob("*"):
            if document.is_file():
                https_server.routes["/" + document.relative_to(root).as_posix()] = Route(
                    body=document.read_bytes()
                )
        source = https_server.url("catalog.json")
    else:
        source = str(root / "catalog.json" if location == "file" else root)

    flags = [*_FLAGS, "--schema-allow-network"] if allow_network else _FLAGS
    result = CliRunner().invoke(
        main,
        [
            *flags,
            source,
            "--schema-map",
            f"{PORTOLAN_URI}={unused}",
            "--schema-map",
            f"{DRAFT_URI}#={path}",
        ],
    )

    assert result.exit_code == 1, result.output
    findings = json.loads(result.output)["findings"]
    schema_findings = [f for f in findings if f["rule_id"] == "PTL-SCH-001"]
    unused_findings = [f for f in findings if f["rule_id"] == "PTL-SCH-002"]
    assert len(unused_findings) == 1
    assert PORTOLAN_URI in unused_findings[0]["message"]
    assert DRAFT_URI in unused_findings[0]["message"]
    assert {f["path"] for f in schema_findings} == {
        "catalog.json",
        "roads/collection.json",
        "roads/segment/segment.json",
    }
    assert all(
        f["rule_id"] == "PTL-SCH-001" and "draft_proof" in f["message"] for f in schema_findings
    )
    assert _trace.counters()["schema.rust_compiles"] == 1


def test_mapping_overrides_bundled_schema_without_cache_leak(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = catalog.write()
    reject = _schema_file(tmp_path, "reject.json", {"required": ["local_proof"]})
    accept = _schema_file(tmp_path, "accept.json", {})
    runner = CliRunner()
    runs = [
        ["--schema-map", f"{PORTOLAN_URI}={reject}"],
        ["--schema-map", f"{PORTOLAN_URI}={accept}"],
        [],
        ["--schema-map", f"{PORTOLAN_URI}={reject}", "--schema-map", f"{PORTOLAN_URI}#={accept}"],
    ]
    for mappings, expected in zip(runs, [1, 0, 0, 0], strict=True):
        result = runner.invoke(main, [*_FLAGS, str(root), *mappings, "--schema-allow-network"])
        assert result.exit_code == expected, result.output
        findings = json.loads(result.output)["findings"]
        assert any(f["rule_id"] == "PTL-SCH-001" for f in findings) == bool(expected)


@pytest.mark.parametrize(
    "value",
    [
        "missing-equals",
        DRAFT_URI,
        "=schema.json",
        "relative=schema.json",
        DRAFT_URI + "=",
        "https://[invalid=schema.json",
        "https://bad uri/schema.json=x",
    ],
)
def test_malformed_schema_map_is_a_usage_error(catalog: CatalogBuilder, value: str) -> None:
    result = CliRunner().invoke(main, [*_FLAGS, str(catalog.write()), "--schema-map", value])
    assert result.exit_code == 2
    assert "--schema-map" in result.output
    assert "URI=PATH" in result.output


@pytest.mark.parametrize(
    "contents, message",
    [
        (None, "cannot read schema file"),
        ("{bad", "invalid JSON"),
        ("[]", "must contain a JSON object"),
        (b"\xff", "invalid JSON"),
    ],
)
def test_bad_schema_files_are_usage_errors(
    catalog: CatalogBuilder, tmp_path: Path, contents: str | bytes | None, message: str
) -> None:
    schema = tmp_path / "schema.json"
    if contents is not None:
        schema.write_bytes(contents.encode() if isinstance(contents, str) else contents)
    result = CliRunner().invoke(
        main, [*_FLAGS, str(catalog.write()), "--schema-map", f"{DRAFT_URI}={schema}"]
    )
    assert result.exit_code == 2
    assert "--schema-map" in result.output
    assert message in result.output


def test_schema_mapping_accepts_an_already_converted_value() -> None:
    value = (DRAFT_URI, {"required": ["proof"]})
    assert SchemaMapping().convert(value, None, None) is value


@pytest.mark.parametrize("schema_flags", [[], ["--no-schema"]])
def test_schema_map_requires_the_profile_pass(
    catalog: CatalogBuilder, tmp_path: Path, schema_flags: list[str]
) -> None:
    path = _schema_file(tmp_path, "schema.json", {})
    result = CliRunner().invoke(
        main,
        ["check", str(catalog.write()), *schema_flags, "--schema-map", f"{PORTOLAN_URI}={path}"],
    )
    assert result.exit_code == 2
    assert "--schema-map requires --schema" in result.output


@pytest.mark.parametrize(
    "unused_uri",
    [
        PORTOLAN_URI.replace("https:", "http:"),
        PORTOLAN_URI + "/",
        PORTOLAN_URI.replace("v0.2.0", "v0.1.0"),
    ],
)
def test_unused_map_warns_even_when_the_bundled_schema_passes(
    catalog: CatalogBuilder, tmp_path: Path, unused_uri: str
) -> None:
    path = _schema_file(tmp_path, "reject.json", {"required": ["local_proof"]})
    result = CliRunner().invoke(
        main, [*_FLAGS, str(catalog.write()), "--schema-map", f"{unused_uri}={path}"]
    )
    assert result.exit_code == 0, result.output
    findings = json.loads(result.output)["findings"]
    schema_findings = [f for f in findings if f["rule_id"].startswith("PTL-SCH-")]
    assert len(schema_findings) == 1
    warning = schema_findings[0]
    assert warning["rule_id"] == "PTL-SCH-002"
    assert warning["severity"] == "warning"
    assert unused_uri in warning["message"]
    assert PORTOLAN_URI in warning["message"]


def test_unused_map_diagnostic_can_be_disabled_without_skipping_validation(
    catalog: CatalogBuilder,
) -> None:
    catalog.overrides["title"] = None
    options = ValidationOptions(
        rules=[],
        structural=False,
        extensions=False,
        data=False,
        schema=True,
        schema_map={DRAFT_URI: {}},
        config=RulesConfig(disabled=frozenset({"PTL-SCH-002"})),
    )
    report = validate(catalog.write(), options=options)
    assert report.errors
    assert all(f.rule_id == "PTL-SCH-001" for f in report.errors)
    assert report.warnings == []


def test_help_states_the_schema_uri_delimiter_limit() -> None:
    result = CliRunner().invoke(main, ["check", "--help"])
    assert result.exit_code == 0
    assert "URI must not contain '='" in " ".join(result.output.split())
