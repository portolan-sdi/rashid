#!/usr/bin/env python3
"""Prove the #266 gates on clean and faulty git-archive copies of HEAD.

Run after committing: uv run python scripts/gate_selftest.py [--fault NAME].
The copies use the caller's environment, with their own src first on PYTHONPATH.
No files in the working tree are changed. Exit 1 means a broken or missed gate.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fault:
    """One syntactically valid edit and the diagnostic its gate must produce."""

    name: str
    path: str
    clean: str
    broken: str
    command: tuple[str, ...]
    diagnostic: str


def _pytest(test: str) -> tuple[str, ...]:
    return ("uv", "run", "pytest", "-n", "8", test, "--no-cov", "-q", "-p", "no:cacheprovider")


_RUFF = (
    "uv",
    "run",
    "ruff",
    "check",
    "--select",
    "TID251",
    "--output-format",
    "json",
    "src/rashid",
)
_LAYERS = ("uv", "run", "lint-imports", "--no-cache")
_REGISTRY = _pytest("tests/unit/test_registration_owner.py")
_API = _pytest("tests/unit/test_api.py::test_public_api_snapshot")
_POINTER = _pytest("tests/unit/test_pointer_construction.py::test_no_handbuilt_pointers_in_src")
_QUALITY = _pytest("tests/unit/test_quality_config.py::test_jscpd_baseline_only_shrinks")
_DUPLICATE_BODY = """
    total = 0
    for value in values:
        if value > limit:
            total += value * 2
        elif value < -limit:
            total -= value * 3
        else:
            total -= value
    if total > limit * 10:
        return limit * 10
    return total
"""
_DUPLICATE_CLEAN = "def first_total(values, limit):" + _DUPLICATE_BODY

FAULTS = (
    *(
        Fault(
            "banned-" + api.replace(".", "-"),
            "src/rashid/_gate_fault.py",
            "",
            f"from {api.rsplit('.', 1)[0]} import {api.rsplit('.', 1)[1]}\n",
            _RUFF,
            "TID251",
        )
        for api in (
            "urllib.parse.urljoin",
            "urllib.parse.urlsplit",
            "urllib.parse.urlparse",
            "urllib.request.Request",
            "urllib.request.urlopen",
            "json.loads",
            "json.load",
            "orjson.loads",
            "obstore.store.HTTPStore",
            "glob.glob",
            "pyarrow.parquet.ParquetFile",
            "rasterio.open",
        )
    ),
    *(
        Fault(
            name,
            "tests/unit/test_gate_fault.py",
            "",
            source,
            (*_RUFF[:-1], "tests/unit/test_gate_fault.py"),
            "use @pytest.mark.xfail(strict=True) keyed to an open issue and listed in the ledger",
        )
        for name, source in (
            ("imperative-xfail-attribute", 'import pytest\npytest.xfail("fault")\n'),
            ("imperative-xfail-import", 'from pytest import xfail\nxfail("fault")\n'),
        )
    ),
    Fault(
        "rule-layer",
        "src/rashid/rules/_gate_fault.py",
        "",
        "import rashid.live\n",
        _LAYERS,
        "metadata rules must not import network passes or transport BROKEN",
    ),
    Fault(
        "file-layer",
        "src/rashid/_gate_fault.py",
        "",
        "import rashid.data._files\n",
        _LAYERS,
        "only data checks may import the file-handle owner BROKEN",
    ),
    Fault(
        "duplicate-block",
        "src/rashid/_gate_fault.py",
        _DUPLICATE_CLEAN,
        _DUPLICATE_CLEAN + "\ndef second_total(values, limit):" + _DUPLICATE_BODY,
        (
            "uvx",
            "jscpd@5.2.1",
            "src/rashid",
            "tests",
            "--baseline",
            ".jscpd-baseline.json",
            "--fail-on-new-clones",
            "0",
        ),
        "new clone",
    ),
    Fault(
        "api-export",
        "src/rashid/api.py",
        '    "roles_of",\n',
        "",
        _API,
        "test_public_api_snapshot[rashid.api]",
    ),
    Fault(
        "package-export",
        "src/rashid/__init__.py",
        '    "Finding",\n',
        "",
        _API,
        "test_public_api_snapshot[rashid]",
    ),
    Fault(
        "api-signature",
        "src/rashid/runner.py",
        "max_documents: int = DEFAULT_MAX_DOCUMENTS",
        "max_documents: int = 9_999",
        _API,
        "test_public_api_snapshot[rashid.api]",
    ),
    Fault(
        "duplicate-rule-id",
        "src/rashid/registry.py",
        'DAT_SIZE = "PTL-DAT-002"',
        'DAT_SIZE = "PTL-DAT-001"',
        _REGISTRY,
        "RegistryError: duplicate rule id",
    ),
    Fault(
        "unregistered-emitter",
        "src/rashid/_gate_fault.py",
        "",
        'from rashid.model import Finding, Severity\nfinding = Finding("PTL-ZZZ-999", Severity.ERROR, "fault", "catalog.json")\n',
        _REGISTRY,
        "test_registered_ids_equal_ids_emitted_in_src",
    ),
    Fault(
        "orphan-registration",
        "src/rashid/data/raster.py",
        "DAT_COG_STATS",
        "DAT_SIZE",
        _REGISTRY,
        "test_registered_ids_equal_ids_emitted_in_src",
    ),
    Fault(
        "copied-pass-id",
        "src/rashid/registry.py",
        "        GEN_MISSING_ROOT,\n",
        '        "PTL-GEN-000",\n',
        _REGISTRY,
        "test_pass_checks_use_the_id_constants_once",
    ),
    *(
        Fault(
            "pointer-" + name,
            "src/rashid/_gate_fault.py",
            "",
            "pointer = " + source + "\n",
            _POINTER,
            "hand-built JSON pointers",
        )
        for name, source in (
            ("fstring", 'f"/assets/{key}"'),
            ("addition", '"/assets/" + key'),
            ("percent", '"/links/%s" % key'),
            ("format", '"/properties/{}".format(key)'),
            ("join", '"/".join(["", "item_assets", key])'),
            ("slash", '"/" + key'),
            ("prefix-slash", 'PREFIX + "/" + key'),
        )
    ),
    Fault(
        "baseline-growth",
        ".jscpd-baseline.json",
        '"fingerprints": {',
        '"fingerprints": {"selftest": 1,',
        _QUALITY,
        "test_jscpd_baseline_only_shrinks",
    ),
    Fault(
        "baseline-swap",
        ".jscpd-baseline.json",
        '"fingerprints": {',
        '"fingerprints": {',  # _write_fault swaps one key without changing counts.
        _QUALITY,
        "baseline adds clone fingerprints",
    ),
    Fault(
        "empty-scan-policy",
        ".jscpd.json",
        '"failOnEmpty": true',
        '"failOnEmpty": false',
        _QUALITY,
        "test_jscpd_baseline_only_shrinks",
    ),
    Fault(
        "loose-xfail",
        "tests/unit/test_gate_fault.py",
        "",
        "import pytest\n@pytest.mark.xfail(strict=False, reason='#999: injected fault')\ndef test_gate_fault(): assert False\n",
        _pytest(
            "tests/unit/test_guardrail_exemptions.py::test_xfails_are_strict_and_match_the_ledger"
        ),
        "xfail must use strict=True",
    ),
    Fault(
        "exemption-growth",
        "guardrail_exemptions.json",
        '"ruff": {',
        '"ruff": {"stale.py": "self-test fault",',
        _pytest("tests/unit/test_guardrail_exemptions.py::test_exemption_counts_and_reasons"),
        "test_exemption_counts_and_reasons",
    ),
    Fault(
        "rules-data-layer",
        "src/rashid/rules/_gate_fault.py",
        "",
        "import rashid.data\n",
        _LAYERS,
        "metadata rules must not import data checks BROKEN",
    ),
    *(
        Fault(
            name,
            "tests/unit/test_gate_fault.py",
            "",
            source,
            _pytest(
                "tests/unit/test_guardrail_exemptions.py::test_xfails_are_strict_and_match_the_ledger"
            ),
            "bare xfail marker",
        )
        for name, source in (
            (
                "bare-xfail-decorator",
                "import pytest\n@pytest.mark.xfail\ndef test_gate_fault(): assert False\n",
            ),
            (
                "bare-xfail-param",
                "import pytest\ncase = pytest.param(1, marks=pytest.mark.xfail)\n",
            ),
        )
    ),
    Fault(
        "extra-ruff-ignore",
        "pyproject.toml",
        '"src/rashid/_href.py" = ["TID251"]',
        '"src/rashid/_href.py" = ["TID251", "ARG001"]',
        _pytest(
            "tests/unit/test_guardrail_exemptions.py::test_ruff_exemptions_match_config_and_banned_uses"
        ),
        "Ruff per-file ignores differ from ledger",
    ),
    Fault(
        "stale-baseline",
        ".jscpd-baseline.json",
        '"fingerprints": {',
        '"fingerprints": {"selftest": 1,',
        ("uv", "run", "python", "scripts/gate_selftest.py", "--check-baseline"),
        "baseline fingerprints differ from current clones",
    ),
    Fault(
        "ci-selftest-wiring",
        ".github/workflows/ci.yml",
        "run: uv run python scripts/gate_selftest.py",
        'run: "true"',
        _pytest("tests/unit/test_quality_config.py::test_ci_runs_the_gate_selftest_on_linux"),
        "test_ci_runs_the_gate_selftest_on_linux",
    ),
    Fault(
        "dropped-fault",
        "scripts/gate_selftest.py",
        "def _copy_" + "head(repo:",
        "FAULTS = FAULTS[1:]\n\ndef _copy_" + "head(repo:",
        ("uv", "run", "python", "scripts/gate_selftest.py", "--help"),
        "expected 43 faults",
    ),
)


def _copy_head(repo: Path, root: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(root, filter="data")


def _run(
    root: Path, command: tuple[str, ...], *, baseline: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_CORE_"))
    }
    env.update(
        UV_NO_SYNC="1",
        UV_PROJECT_ENVIRONMENT=sys.prefix,
        VIRTUAL_ENV=sys.prefix,
        PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))),
        NO_COLOR="1",
    )
    if baseline is not None:
        env["RASHID_BASELINE_PATH"] = str(baseline)
    return subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, check=False, timeout=180
    )


def _check_baseline(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rashid-clones-") as directory:
        baseline = Path(directory) / "baseline.json"
        run = _run(
            root,
            (
                "uvx",
                "jscpd@5.2.1",
                "src/rashid",
                "tests",
                "--baseline",
                str(baseline),
                "--update-baseline",
                "--reporters",
                "silent",
            ),
        )
        if run.returncode:
            raise RuntimeError(run.stdout + run.stderr)
        expected = json.loads((root / ".jscpd-baseline.json").read_text(encoding="utf-8"))
        current = json.loads(baseline.read_text(encoding="utf-8"))
        assert current["fingerprints"] == expected["fingerprints"], (
            "baseline fingerprints differ from current clones"
        )


def _write_fault(root: Path, fault: Fault, *, broken: bool) -> None:
    path = root / fault.path
    new = fault.broken if broken else fault.clean
    if path.exists():
        old = path.read_text(encoding="utf-8")
        # The orphan fault changes all uses so no emitter of DAT_COG_STATS remains.
        if fault.name != "orphan-registration" and old.count(fault.clean) != 1:
            raise ValueError(f"{fault.path}: expected one occurrence of {fault.clean!r}")
        if fault.clean not in old:
            raise ValueError(f"{fault.path}: fault no longer applies")
        new = old.replace(fault.clean, new)
    if fault.name == "baseline-swap" and broken:
        data = json.loads(new)
        fingerprints = data["fingerprints"]
        fingerprints["selftest"] = fingerprints.pop(next(iter(fingerprints)))
        new = json.dumps(data, indent=2) + "\n"
    path.write_text(new, encoding="utf-8")
    if path.suffix == ".py":
        compile(new, str(path), "exec")


def exercise(repo: Path, fault: Fault) -> bool:
    """A clean pass and the expected fault diagnostic are both required."""
    with tempfile.TemporaryDirectory(prefix="rashid-gate-") as directory:
        root = Path(directory) / "repo"
        root.mkdir()
        _copy_head(repo, root)
        baseline = Path(directory) / "approved-baseline.json"
        baseline.write_bytes((root / ".jscpd-baseline.json").read_bytes())
        _write_fault(root, fault, broken=False)
        clean = _run(root, fault.command, baseline=baseline)
        if clean.returncode:
            print(
                f"BROKEN {fault.name}: clean copy failed\n{clean.stdout}{clean.stderr}", flush=True
            )
            return False
        # Restore HEAD before applying a fault to an existing file.
        (root / fault.path).unlink()
        _copy_head(repo, root)
        _write_fault(root, fault, broken=True)
        broken = _run(root, fault.command, baseline=baseline)
        caught = broken.returncode != 0 and fault.diagnostic in broken.stdout + broken.stderr
        print(f"{'CAUGHT' if caught else 'MISSED'} {fault.name}", flush=True)
        if not caught:
            print(broken.stdout + broken.stderr, flush=True)
        return caught


def main() -> int:
    assert len(FAULTS) == 43, f"expected 43 faults, found {len(FAULTS)}"
    assert len({fault.name for fault in FAULTS}) == 43, "fault names must be unique"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault", choices=[fault.name for fault in FAULTS])
    parser.add_argument(
        "--check-baseline", action="store_true", help="Check live clone fingerprints only"
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.check_baseline:
        _check_baseline(repo)
        return 0
    faults = [fault for fault in FAULTS if args.fault in (None, fault.name)]
    results = []
    for fault in faults:
        try:
            results.append(exercise(repo, fault))
        except (OSError, ValueError, subprocess.SubprocessError, SyntaxError) as exc:
            print(f"ERROR {fault.name}: {exc}", flush=True)
            results.append(False)
    print(f"{sum(results)}/{len(results)} faults caught with clean copies green")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
