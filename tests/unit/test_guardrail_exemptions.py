"""Keep the exemption ledger equal to the config and to live source uses."""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
EXEMPTIONS = json.loads((ROOT / "guardrail_exemptions.json").read_text(encoding="utf-8"))


def test_exemption_counts_and_reasons() -> None:
    assert {kind: len(rows) for kind, rows in EXEMPTIONS.items()} == {
        "ruff": 6,
        "import-linter": 1,
        "allowed-importers": 5,
        "jscpd": 1,
        "xfail": 0,
    }
    for kind, rows in EXEMPTIONS.items():
        reasons = (
            [reason for edges in rows.values() for reason in edges.values()]
            if kind in {"ruff", "import-linter"}
            else rows.values()
        )
        assert all(isinstance(reason, str) and reason.strip() for reason in reasons)
    assert sum(len(edges) for edges in EXEMPTIONS["import-linter"].values()) == 1


def test_ruff_exemptions_match_config_and_banned_uses() -> None:
    ignores = CONFIG["tool"]["ruff"]["lint"]["per-file-ignores"]
    assert ignores == {path: list(codes) for path, codes in EXEMPTIONS["ruff"].items()}, (
        "Ruff per-file ignores differ from ledger"
    )
    run = subprocess.run(
        [
            "uv",
            "run",
            "ruff",
            "check",
            "--select",
            "TID251,ARG002,ARG005",
            "--ignore-noqa",
            "--config",
            "lint.per-file-ignores={}",
            "--output-format",
            "json",
            "src/rashid",
            "tests",
            "scripts",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1, run.stderr
    uses = {
        (Path(row["filename"]).relative_to(ROOT).as_posix(), row["code"])
        for row in json.loads(run.stdout)
    }
    for pattern, codes in EXEMPTIONS["ruff"].items():
        for code in codes:
            assert any(
                fnmatch.fnmatch(path, pattern) and reported == code for path, reported in uses
            ), f"stale exemption: {pattern} {code}"


def _imports(module: str) -> set[str]:
    path = ROOT / "src" / module.replace(".", "/")
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    imports = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            imports.update(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def test_import_exemptions_match_config_and_live_edges() -> None:
    contracts = CONFIG["tool"]["importlinter"]["contracts"]
    assert {
        contract["name"]: set(contract["ignore_imports"])
        for contract in contracts
        if contract.get("ignore_imports")
    } == {name: set(edges) for name, edges in EXEMPTIONS["import-linter"].items()}
    for edges in EXEMPTIONS["import-linter"].values():
        for edge in edges:
            importer, imported = edge.split(" -> ")
            assert imported in _imports(importer), f"stale exemption: {edge}"
    protected = next(contract for contract in contracts if contract.get("id") == "data-files-owner")
    assert set(protected["allowed_importers"]) == set(EXEMPTIONS["allowed-importers"])
    for importer in EXEMPTIONS["allowed-importers"]:
        assert "rashid.data._files" in _imports(importer), f"stale importer: {importer}"


def test_baseline_exemption_matches_a_nonempty_baseline() -> None:
    assert set(EXEMPTIONS["jscpd"]) == {".jscpd-baseline.json"}
    baseline = json.loads((ROOT / ".jscpd-baseline.json").read_text(encoding="utf-8"))
    assert baseline["fingerprints"]


def xfail_issues(source: str) -> set[str]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    issues = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "xfail"):
            continue
        call = parents.get(node)
        assert isinstance(call, ast.Call) and call.func is node, (
            f"bare xfail marker at line {node.lineno}"
        )
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert (
            isinstance(keywords.get("strict"), ast.Constant) and keywords["strict"].value is True
        ), "xfail must use strict=True"
        reason = keywords.get("reason")
        assert isinstance(reason, ast.Constant) and isinstance(reason.value, str)
        match = re.match(r"#\d+\b", reason.value)
        assert match, f"xfail must name its issue at line {node.lineno}"
        issues.add(match[0])
    return issues


def test_xfails_are_strict_and_match_the_ledger() -> None:
    issues = set()
    for path in (ROOT / "tests").rglob("*.py"):
        if "fixtures" not in path.relative_to(ROOT / "tests").parts:
            issues.update(xfail_issues(path.read_text(encoding="utf-8")))
    assert issues == set(EXEMPTIONS["xfail"])


@pytest.mark.parametrize(
    "source",
    [
        "@pytest.mark.xfail\ndef test_x(): pass",
        "case = pytest.param(1, marks=pytest.mark.xfail)",
        "case = pytest.param(1, marks=[pytest.mark.xfail])",
    ],
)
def test_bare_xfail_markers_are_rejected(source: str) -> None:
    with pytest.raises(AssertionError, match="bare xfail marker"):
        xfail_issues(source)


def test_strict_xfail_call_names_its_issue() -> None:
    assert xfail_issues(
        "@pytest.mark.xfail(strict=True, reason='#999: example')\ndef test_x(): pass"
    ) == {"#999"}
