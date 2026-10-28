"""Measure one seeded scenario: counters, time, memory, imports, or instructions.

    uv run python scripts/profile.py <scenario> [--size small|large] --mode MODE

Modes:

``counts``
    Run once with timings on. Print every counter and the span table. Needs
    nothing beyond the dev environment.
``time``
    Run once under pyinstrument and print its call tree.
``mem``
    Run once under memray, then print ``memray stats``. Linux and macOS only.
``import``
    Print the 15 slowest imports of ``rashid.cli`` from ``python -X importtime``.
``instr``
    Run once under ``valgrind --tool=cachegrind`` and print the instruction
    count. The count varies far less between runs than wall time does.
``run``
    Run once with no profiler. The other modes wrap this one.

Scenarios are built once and cached under ``.cache/scenarios`` (see
``tests/scenarios.py``). Profiler output goes to ``.cache/profiles``. The
profilers load through ``uv run --with`` at a pinned version, so neither is a
dependency of rashid.

``--warm`` runs the scenario once before the measured run. The first run in a
process also loads the bundled schemas; ``--warm`` leaves that cost out.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PYINSTRUMENT = "pyinstrument==5.1.3"
MEMRAY = "memray==1.20.0"
MODES = ("counts", "time", "mem", "import", "instr", "run")
_PROFILES = REPO / ".cache" / "profiles"

# "import time:       437 |      34470 |       rashid._store"
_IMPORT_LINE = re.compile(r"^import time:\s+(\d+) \|\s+(\d+) \|( *)(\S+)")
# "==12345== I   refs:      1,234,567"
_CACHEGRIND_REFS = re.compile(r"I\s+refs:\s+([\d,]+)")


def _run_once(scenario: str, size: str, seed: int, warm: bool) -> None:
    import rashid
    from tests import scenarios

    with scenarios.target(scenario, size, seed) as target:
        if warm:
            rashid.validate(target.location, **target.options)
        rashid.validate(target.location, **target.options)


def _counts(scenario: str, size: str, seed: int, warm: bool) -> None:
    from rashid import _trace

    _trace.enable("on")
    _run_once(scenario, size, seed, warm=False)
    if warm:
        _trace.reset()
        _run_once(scenario, size, seed, warm=False)
    print(f"# {scenario}/{size} seed={seed}{' (warm)' if warm else ''}")
    for name, value in _trace.counters().items():
        print(f"{name:<28} {value:>12,}")
    print()
    print(_trace.format_timings(_trace.timings()))


def _self_argv(scenario: str, size: str, seed: int, warm: bool) -> list[str]:
    argv = [str(Path(__file__).resolve()), scenario, "--size", size, "--seed", str(seed)]
    return [*argv, "--mode", "run", *(["--warm"] if warm else [])]


def profiler_command(
    mode: str, scenario: str, size: str, seed: int, warm: bool, out: Path
) -> list[str]:
    """The argv that runs this script's ``run`` mode under the profiler for ``mode``."""
    target = _self_argv(scenario, size, seed, warm)
    if mode == "time":
        return [
            *("uv", "run", "--with", PYINSTRUMENT, "python", "-m", "pyinstrument"),
            *("-r", "text", "--show", "*/rashid/*", "-o", str(out), *target),
        ]
    if mode == "mem":
        return [
            *("uv", "run", "--with", MEMRAY, "python", "-m", "memray", "run"),
            *("-q", "-f", "--native", "-o", str(out), *target),
        ]
    if mode == "instr":
        return [
            *("valgrind", "--tool=cachegrind", "--cache-sim=no"),
            f"--cachegrind-out-file={out}",
            sys.executable,
            *target,
        ]
    raise ValueError(f"no profiler for mode {mode!r}")


def parse_importtime(stderr: str) -> list[tuple[int, int, str]]:
    """``(cumulative_us, self_us, module)`` for each top-level import line, slowest first."""
    rows = []
    for line in stderr.splitlines():
        match = _IMPORT_LINE.match(line)
        if match is not None:
            rows.append((int(match[2]), int(match[1]), match[4]))
    return sorted(rows, reverse=True)


def parse_cachegrind(stderr: str) -> int | None:
    """The total instruction count from cachegrind's summary, or None when absent."""
    match = _CACHEGRIND_REFS.search(stderr)
    return None if match is None else int(match[1].replace(",", ""))


def _imports() -> None:
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", "import rashid.cli"],
        capture_output=True,
        text=True,
        check=True,
    )
    rows = parse_importtime(result.stderr)
    total = next((cum for cum, _self, name in rows if name == "rashid.cli"), None)
    print(f"# import rashid.cli: {total / 1000:.1f} ms" if total else "# import rashid.cli")
    print(f"{'cumulative ms':>13}  {'self ms':>8}  module")
    for cumulative, own, name in rows[:15]:
        print(f"{cumulative / 1000:>13.1f}  {own / 1000:>8.1f}  {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure one seeded rashid scenario.",
        epilog="See tests/scenarios.py for what each scenario holds.",
    )
    parser.add_argument("scenario", choices=("metadata", "data", "remote", "remote-data"))
    parser.add_argument("--size", choices=("small", "large"), default="small")
    parser.add_argument("--mode", choices=MODES, default="counts")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--warm", action="store_true", help="run once before measuring")
    args = parser.parse_args(argv)

    if args.mode == "run":
        _run_once(args.scenario, args.size, args.seed, args.warm)
        return 0
    if args.mode == "counts":
        _counts(args.scenario, args.size, args.seed, args.warm)
        return 0
    if args.mode == "import":
        _imports()
        return 0

    if args.mode == "mem" and sys.platform == "win32":
        print("memray does not run on Windows; use --mode counts for peak RSS.", file=sys.stderr)
        return 2
    if args.mode == "instr" and shutil.which("valgrind") is None:
        print("valgrind is not installed; --mode instr needs it (Linux).", file=sys.stderr)
        return 2

    from tests import scenarios

    scenarios.cached(args.scenario, args.size, args.seed)  # build outside the profiler
    _PROFILES.mkdir(parents=True, exist_ok=True)
    stem = f"{args.scenario}-{args.size}-{args.seed}{'-warm' if args.warm else ''}"
    suffix = {"mem": "bin", "instr": "cachegrind", "time": "txt"}[args.mode]
    out = _PROFILES / f"{stem}.{args.mode}.{suffix}"
    command = profiler_command(args.mode, args.scenario, args.size, args.seed, args.warm, out)
    print("$", " ".join(command), file=sys.stderr)
    if args.mode == "instr":
        result = subprocess.run(command, capture_output=True, text=True, env=_instr_env())
        refs = parse_cachegrind(result.stderr)
        if result.returncode != 0 or refs is None:
            sys.stderr.write(result.stderr)
            return result.returncode or 1
        print(f"# {stem}: {refs:,} instructions (cachegrind Ir); details in {out}")
        return 0
    result = subprocess.run(command)
    if result.returncode != 0:
        return result.returncode
    if args.mode == "time":
        print(out.read_text(encoding="utf-8"))
        print(f"# saved to {out}")
        return 0
    stats = ["uv", "run", "--with", MEMRAY, "python", "-m", "memray", "stats", str(out)]
    return subprocess.run(stats).returncode


def _instr_env() -> dict[str, str]:
    """A fixed hash seed, so set and dict order does not move the instruction count."""
    import os

    return {**os.environ, "PYTHONHASHSEED": "0"}


if __name__ == "__main__":
    raise SystemExit(main())
