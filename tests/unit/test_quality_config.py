"""Pin the owner boundaries and prevent gate settings from weakening silently."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
JSCPD_BASELINE_FINGERPRINTS = 31
JSCPD_BASELINE_CLONES = 31


def test_tid251_bans_the_owner_apis() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lint = config["tool"]["ruff"]["lint"]
    assert "TID251" in lint["select"] + lint["extend-select"]
    assert lint["flake8-tidy-imports"]["banned-api"] == {
        "pytest.xfail": {
            "msg": "use @pytest.mark.xfail(strict=True) keyed to an open issue and listed in the ledger"
        },
    }
    assert "TID251" not in lint["per-file-ignores"]["tests/**"]
    source = tomllib.loads((ROOT / "src/ruff.toml").read_text(encoding="utf-8"))
    assert source["extend"] == "../pyproject.toml"
    banned = source["lint"]["flake8-tidy-imports"]["extend-banned-api"]
    assert all(entry["msg"].strip() for entry in banned.values())


def test_owner_layer_contracts_cover_new_modules() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = {row.get("id"): row for row in config["tool"]["importlinter"]["contracts"]}
    rules = contracts["rules-no-network"]
    assert rules["type"] == "forbidden"
    assert not rules.get("ignore_imports")
    assert rules["source_modules"] == ["rashid.rules"]
    assert set(rules["forbidden_modules"]) == {"rashid.live", "rashid.remote", "rashid._store"}
    assert rules.get("as_packages", True) is True
    assert rules.get("allow_indirect_imports", False) is False
    data = contracts["rules-no-data"]
    assert data["type"] == "forbidden"
    assert data["source_modules"] == ["rashid.rules"]
    assert data["forbidden_modules"] == ["rashid.data"]
    assert not data.get("ignore_imports")
    assert data.get("as_packages", True) is True
    assert data.get("allow_indirect_imports", False) is False
    files = contracts["data-files-owner"]
    assert files["type"] == "protected"
    assert files["protected_modules"] == ["rashid.data._files"]
    assert files["as_packages"] is False


def test_jscpd_baseline_only_shrinks() -> None:
    """Delete only: lower the pin after deduplication, never raise it."""
    config = json.loads((ROOT / ".jscpd.json").read_text(encoding="utf-8"))
    assert config["minTokens"] == 50
    assert config["failOnEmpty"] is True
    assert config["format"] == ["python"]
    assert set(config["ignore"]) == {"**/tests/fixtures/**", "**/.venv/**"}
    baseline = json.loads((ROOT / ".jscpd-baseline.json").read_text(encoding="utf-8"))
    assert len(baseline["fingerprints"]) == JSCPD_BASELINE_FINGERPRINTS
    assert sum(baseline["fingerprints"].values()) == JSCPD_BASELINE_CLONES
    approved_path = os.environ.get("RASHID_BASELINE_PATH")
    if approved_path:
        approved = json.loads(Path(approved_path).read_text(encoding="utf-8"))
    else:
        reference = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if reference.returncode:
            pytest.skip("origin/main is unavailable; cannot compare baseline fingerprints")
        listed = subprocess.run(
            ["git", "ls-tree", "--name-only", "origin/main", "--", ".jscpd-baseline.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        if not listed.stdout.strip():
            return  # The first baseline has no previous fingerprints to compare.
        previous = subprocess.run(
            ["git", "show", "origin/main:.jscpd-baseline.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        approved = json.loads(previous.stdout)
    assert Counter(baseline["fingerprints"]) <= Counter(approved["fingerprints"]), (
        "baseline adds clone fingerprints"
    )


def test_jscpd_baseline_skips_when_base_ref_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RASHID_BASELINE_PATH", raising=False)

    def git(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(128, command)
        return subprocess.CompletedProcess(command, 128, stdout="", stderr="unknown revision")

    monkeypatch.setattr(subprocess, "run", git)
    with pytest.raises(pytest.skip.Exception, match="origin/main.*unavailable"):
        test_jscpd_baseline_only_shrinks()


@pytest.mark.parametrize("change", ["replace", "count-transfer"])
def test_jscpd_baseline_rejects_unapproved_fingerprints(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.delenv("RASHID_BASELINE_PATH", raising=False)
    approved = json.loads((ROOT / ".jscpd-baseline.json").read_text(encoding="utf-8"))
    fingerprints = approved["fingerprints"]
    first, second = list(fingerprints)[:2]
    if change == "replace":
        fingerprints["other-clone"] = fingerprints.pop(first)
    else:
        fingerprints[first] -= 1
        fingerprints[second] += 1

    def git(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = ".jscpd-baseline.json\n" if command[1] == "ls-tree" else json.dumps(approved)
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(subprocess, "run", git)
    with pytest.raises(AssertionError, match="baseline adds clone fingerprints"):
        test_jscpd_baseline_only_shrinks()

    # An actual deletion remains allowed, including multiple copies of a clone.
    fingerprints.update(
        json.loads((ROOT / ".jscpd-baseline.json").read_text(encoding="utf-8"))["fingerprints"]
    )
    fingerprints[first] += 1
    test_jscpd_baseline_only_shrinks()


def test_jscpd_hook_runs_in_the_ci_commit_stage() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    (hook,) = [hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "jscpd"]
    command = shlex.split(hook["entry"])
    assert command[0] == "uvx"
    assert command[1].startswith("jscpd@") and command[1].removeprefix("jscpd@")
    assert command[2:] == [
        "src/rashid",
        "tests",
        "--baseline",
        ".jscpd-baseline.json",
        "--fail-on-new-clones",
        "0",
    ]
    assert hook["pass_filenames"] is False
    assert hook["language"] == "system"
    assert hook["stages"] == ["pre-commit"]
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    # This pinned reusable workflow runs prek against both hook stages.
    workflow, _, ref = ci["jobs"]["ci"]["uses"].partition("@")
    assert workflow == "portolan-sdi/portolan-ops/.github/workflows/reusable-python-ci.yml"
    assert ref
    assert "pytest -n 8" in ci["jobs"]["ci"]["with"]["test-command"]


def test_ci_runs_the_gate_selftest_on_linux() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = ci["jobs"]["guardrail-selftest"]
    assert job["runs-on"] == "ubuntu-latest"
    assert not job.get("if")
    assert not job.get("continue-on-error", False)
    steps = job["steps"]
    setup = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    )
    install = next(
        index for index, step in enumerate(steps) if "uv sync --locked" in step.get("run", "")
    )
    run = next(
        index
        for index, step in enumerate(steps)
        if step.get("run") == "uv run python scripts/gate_selftest.py"
    )
    assert setup < install < run
    assert not steps[run].get("if")
    assert not steps[run].get("continue-on-error", False)
