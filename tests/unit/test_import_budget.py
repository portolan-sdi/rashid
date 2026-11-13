"""What ``import rashid`` and ``rashid --help`` load, checked in a fresh interpreter.

import-linter proves that no rashid module imports the geospatial stack at
module level. It cannot see an import that a third-party package makes, or
one that a module-level call triggers. This test reads ``sys.modules`` after
the import, so it catches both. The pytest process has already imported
everything, so each case runs in a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Loaded only by the passes that need them, never by an import or by --help.
_HEAVY = frozenset(
    {
        "pyarrow",
        "rasterio",
        "pyproj",
        "rio_cogeo",
        "shapely",
        "numpy",
        "jsonschema",
        "jsonschema_rs",
        "referencing",
        "jsonschema_specifications",
    }
)
# Profilers run the code; the code never imports them.
_PROFILERS = frozenset({"pyinstrument", "memray"})
# Loaded on the first HTTPS request. A --no-data check of a directory sends none.
_NETWORK = frozenset({"obstore", "ssl", "urllib.request"})
# Read on the first request, for the User-Agent header, or for --version and --timings.
_METADATA = frozenset({"importlib.metadata"})

_REPORT = "import sys, json; print(json.dumps(sorted(sys.modules)))"


def _loaded(code: str) -> set[str]:
    # Retain the OS environment (especially Windows' SYSTEMROOT for SSL),
    # while excluding Python startup and rashid options that affect imports.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "RASHID_"))}
    env["PATH"] = ""
    result = subprocess.run(
        [sys.executable, "-c", f"{code}\n{_REPORT}"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    names = json.loads(result.stdout.splitlines()[-1])
    # Each module by its full name and by its package, so a budget can name
    # ``urllib.request`` without naming all of ``urllib``.
    return {*names, *(name.split(".")[0] for name in names)}


def test_import_rashid_loads_no_heavy_module_and_no_cli_framework() -> None:
    loaded = _loaded("import rashid")
    assert "rashid" in loaded
    assert loaded & (_HEAVY | _PROFILERS | _NETWORK | _METADATA | {"click"}) == set()


def test_import_probe_reports_child_stderr() -> None:
    with pytest.raises(AssertionError, match="RuntimeError: import-probe-failure"):
        _loaded("raise RuntimeError('import-probe-failure')")


def test_import_probe_preserves_platform_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    system_root = os.environ.get("SYSTEMROOT", "platform-system-root")
    monkeypatch.setenv("SYSTEMROOT", system_root)
    monkeypatch.setenv("PYTHONPATH", "must-not-reach-child")
    monkeypatch.setenv("RASHID_TIMINGS", "mem")
    code = (
        "import os\n"
        f"assert os.environ.get('SYSTEMROOT') == {system_root!r}\n"
        "assert os.environ['PATH'] == ''\n"
        "assert 'PYTHONPATH' not in os.environ\n"
        "assert 'RASHID_TIMINGS' not in os.environ\n"
    )
    assert "os" in _loaded(code)


def test_cli_help_loads_no_heavy_module() -> None:
    code = (
        "import contextlib, io\n"
        "from rashid.cli import main\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    try:\n"
        "        main(['check', '--help'])\n"
        "    except SystemExit as exc:\n"
        "        assert exc.code == 0, exc.code\n"
    )
    loaded = _loaded(code)
    assert "click" in loaded
    assert loaded & (_HEAVY | _PROFILERS | _NETWORK | _METADATA) == set()


def test_a_local_metadata_check_loads_no_network_stack() -> None:
    """A ``--no-data`` check of a directory sends no request, so it loads no transport."""
    catalog = Path(__file__).parents[1] / "fixtures" / "reference-catalog"
    code = (
        "import contextlib, io\n"
        "from rashid.cli import main\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    try:\n"
        f"        main(['check', '--no-data', {str(catalog)!r}])\n"
        "    except SystemExit:\n"
        "        pass\n"
    )
    assert _loaded(code) & (_NETWORK | _METADATA) == set()


@pytest.mark.parametrize("module", ["rashid.data.checks"])
def test_the_budget_detects_a_heavy_import(module: str) -> None:
    """The check itself can fail: the data checks module loads the geo stack."""
    pytest.importorskip("pyarrow")
    assert {"pyarrow", "rasterio"} <= _loaded(f"import {module}")


def test_the_budget_detects_the_network_stack() -> None:
    """The first store loads obstore. Building it sends no request."""
    loaded = _loaded("from rashid import _store\n_store._store('https://example.org')")
    assert "obstore" in loaded


def test_the_first_urllib_request_loads_ssl() -> None:
    """The urllib transport loads ``ssl`` and ``urllib.request`` when it builds its opener."""
    loaded = _loaded("from rashid import _store\n_store._opener()")
    assert {"ssl", "urllib.request"} <= loaded
