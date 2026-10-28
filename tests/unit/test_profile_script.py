"""scripts/profile.py: its parsers, its profiler commands, and its refusals.

The time and mem modes download a pinned profiler through ``uv run --with``,
so these tests check the command they build instead of running it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "profile.py"


@pytest.fixture(scope="module")
def profile() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rashid_profile_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_importtime_sorts_by_cumulative_time(profile: ModuleType) -> None:
    stderr = "\n".join(
        [
            "import time: self [us] | cumulative | imported package",
            "import time:       437 |      34470 |       rashid._store",
            "import time:       326 |      93989 | rashid",
            "noise",
            "import time:      1351 |      13630 |         urllib.request",
        ]
    )
    assert profile.parse_importtime(stderr) == [
        (93989, 326, "rashid"),
        (34470, 437, "rashid._store"),
        (13630, 1351, "urllib.request"),
    ]


def test_parse_cachegrind_reads_the_instruction_total(profile: ModuleType) -> None:
    stderr = "==4242== Cachegrind, a high-precision tracing profiler\n==4242== I   refs:      1,234,567\n"
    assert profile.parse_cachegrind(stderr) == 1_234_567
    assert profile.parse_cachegrind("no summary") is None


def test_time_runs_the_script_under_pinned_pyinstrument(
    profile: ModuleType, tmp_path: Path
) -> None:
    command = profile.profiler_command("time", "remote", "large", 3, True, tmp_path / "x")
    assert command[command.index("-o") + 1] == str(tmp_path / "x")
    assert command[:7] == [
        "uv",
        "run",
        "--with",
        "pyinstrument==5.1.3",
        "python",
        "-m",
        "pyinstrument",
    ]
    assert command[-9:] == [
        str(_SCRIPT),
        "remote",
        "--size",
        "large",
        "--seed",
        "3",
        "--mode",
        "run",
        "--warm",
    ]


def test_mem_writes_to_the_output_file(profile: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "m.bin"
    command = profile.profiler_command("mem", "data", "small", 0, False, out)
    assert command[2:4] == ["--with", "memray==1.20.0"]
    assert command[command.index("-o") + 1] == str(out)
    assert command[-1] == "run"


def test_instr_runs_this_interpreter_under_cachegrind(profile: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "c.out"
    command = profile.profiler_command("instr", "metadata", "small", 0, False, out)
    assert command[:3] == ["valgrind", "--tool=cachegrind", "--cache-sim=no"]
    assert command[3] == f"--cachegrind-out-file={out}"
    assert command[4] == sys.executable


def test_no_profiler_for_counts(profile: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no profiler"):
        profile.profiler_command("counts", "metadata", "small", 0, False, tmp_path)


def test_instr_without_valgrind_exits_2(
    profile: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(profile.shutil, "which", lambda name: None)
    assert profile.main(["metadata", "--mode", "instr"]) == 2
    assert "valgrind is not installed" in capsys.readouterr().err


def test_mem_on_windows_exits_2(
    profile: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(profile.sys, "platform", "win32")
    assert profile.main(["metadata", "--mode", "mem"]) == 2
    assert "memray does not run on Windows" in capsys.readouterr().err


def test_counts_prints_counters_and_the_span_table(
    profile: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests import scenarios

    monkeypatch.setattr(scenarios, "_CACHE", tmp_path)
    assert profile.main(["metadata", "--mode", "counts", "--warm"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# metadata/small seed=0 (warm)"
    assert out[1].split() == ["json.parses", "103"]
    assert any(line.startswith("rules ") for line in out)


def test_import_mode_names_the_cli_total(
    profile: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert profile.main(["metadata", "--mode", "import"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("# import rashid.cli: ")
    assert out[1].split() == ["cumulative", "ms", "self", "ms", "module"]
    assert out[2].split()[-1] == "rashid.cli"
