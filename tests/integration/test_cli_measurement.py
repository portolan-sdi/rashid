"""The root CLI flags for logging and timings, and the ``meta`` block they add.

Log records and the timings table go to stderr. stdout carries only the
report, so ``rashid check --json`` stays parseable at every verbosity.
"""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner

from rashid import _trace
from rashid.cli import main
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.integration


@pytest.fixture
def root(catalog: CatalogBuilder) -> Path:
    catalog.collection("roads")
    return catalog.write()


def _check(*args: str) -> list[str]:
    return [*args, "check", "--no-data"]


def test_plain_json_has_no_meta(root: Path) -> None:
    result = CliRunner().invoke(main, [*_check(), "--json", str(root)])
    payload = json.loads(result.stdout)
    assert "meta" not in payload
    assert result.stderr == ""


def test_timings_add_meta_to_json(root: Path) -> None:
    # The first run in a process parses the bundled schemas into lru_cache
    # registries. Warm up, so the count below does not depend on test order.
    CliRunner().invoke(main, [*_check(), "--json", str(root)])
    _trace.reset()
    result = CliRunner().invoke(main, [*_check("--timings"), "--json", str(root)])
    meta = json.loads(result.stdout)["meta"]
    assert meta["version"] == version("rashid")
    assert meta["run_id"] == _trace.RUN_ID
    (check,) = meta["timings"]
    assert check["name"] == "check"
    stages = [child["name"] for child in check["children"]]
    assert stages == ["load", "rules", "structural", "extensions"]
    # The root catalog and the one collection.
    assert meta["counters"]["json.parses"] == 2
    assert result.stderr == ""


def test_timings_print_a_table_to_stderr_in_text_mode(root: Path) -> None:
    result = CliRunner().invoke(main, [*_check("--timings"), str(root)])
    assert "no findings" in result.stdout
    assert "span" not in result.stdout
    lines = result.stderr.splitlines()
    assert lines[0].split()[:2] == ["span", "calls"]
    assert lines[1].startswith("check ")
    assert any(line.startswith("  rules ") for line in lines)


@pytest.mark.parametrize(("flag", "debug"), [("-vv", True), ("-v", False), ("--quiet", False)])
def test_verbosity_sets_the_stderr_level(root: Path, flag: str, debug: bool) -> None:
    result = CliRunner().invoke(main, [*_check(flag), "--json", str(root)])
    json.loads(result.stdout)
    assert (" DEBUG rashid.runner: rules: start" in result.stderr) is debug
    # rashid logs no INFO record on a clean local run, so -v adds nothing here.
    assert (result.stderr == "") is not debug


def test_verbose_and_quiet_conflict(root: Path) -> None:
    result = CliRunner().invoke(main, [*_check("-v", "-q"), str(root)])
    assert result.exit_code == 2
    assert "--verbose and --quiet ask for opposite things" in result.output


def test_verbose_keeps_the_log_file_from_the_environment(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "run.jsonl"
    monkeypatch.setenv("RASHID_LOG_FILE", str(log_file))
    CliRunner().invoke(main, [*_check("-v"), str(root)])
    _trace.configure_logging(None)
    messages = [json.loads(line)["message"] for line in log_file.read_text().splitlines()]
    assert "rules: start" in messages


def test_trace_flag_writes_a_chrome_trace(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits: list[object] = []
    monkeypatch.setattr(_trace, "_trace_paths", [])
    monkeypatch.setattr(_trace.atexit, "register", exits.append)
    out = tmp_path / "trace.json"
    CliRunner().invoke(main, [*_check("--trace", str(out)), str(root)])
    assert exits == [_trace._write_traces]
    _trace._write_traces()
    events = json.loads(out.read_text())["traceEvents"]
    assert {"check", "load", "rules"} <= {e["name"] for e in events if e["ph"] == "X"}


def test_trace_alone_adds_no_meta_and_no_table(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_trace, "_trace_paths", [])
    monkeypatch.setattr(_trace.atexit, "register", lambda fn: None)
    out = str(tmp_path / "trace.json")
    as_json = CliRunner().invoke(main, [*_check("--trace", out), "--json", str(root)])
    assert "meta" not in json.loads(as_json.stdout)
    as_text = CliRunner().invoke(main, [*_check("--trace", out), str(root)])
    assert as_json.stderr == as_text.stderr == ""
    assert _trace.enabled()


def test_timings_from_the_environment_add_meta(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASHID_TIMINGS", "1")
    result = CliRunner().invoke(main, [*_check(), "--json", str(root)])
    assert json.loads(result.stdout)["meta"]["timings"][0]["name"] == "check"


def test_trace_into_a_missing_directory_fails_before_the_run(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "missing" / "trace.json"
    result = CliRunner().invoke(main, [*_check("--trace", str(out)), str(root)])
    assert result.exit_code == 2
    assert "Invalid value for --trace" in result.output
    assert "does not exist" in result.output
    assert "files checked" not in result.output


def test_verbose_ignores_an_unopenable_log_file_as_the_import_does(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = str(tmp_path / "missing" / "run.jsonl")
    monkeypatch.setenv("RASHID_LOG_FILE", log_file)
    # The import-time path reports the bad file first, as it does in a real run.
    _trace.configure_from_env({"RASHID_LOG_FILE": log_file})
    result = CliRunner().invoke(main, [*_check("-vv"), str(root)])
    assert result.exit_code == 0
    assert "no findings" in result.stdout
    assert "ignoring RASHID_LOG/RASHID_LOG_FILE" not in result.stderr
    assert " DEBUG rashid.runner: rules: start" in result.stderr


def test_verbose_reports_an_unopenable_log_file_once(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RASHID_LOG_FILE", str(tmp_path / "missing" / "run.jsonl"))
    result = CliRunner().invoke(main, [*_check("-v"), str(root)])
    assert result.exit_code == 0
    assert result.stderr.count("rashid: ignoring RASHID_LOG/RASHID_LOG_FILE:") == 1


def test_help_says_verbose_does_not_change_findings() -> None:
    result = CliRunner().invoke(main, ["--help"])
    text = " ".join(result.stdout.split())
    assert "it does not change which findings print" in text
    for flag in ("--timings", "--trace PATH", "--quiet"):
        assert flag in text
