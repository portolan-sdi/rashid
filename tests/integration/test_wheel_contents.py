"""The built wheel must carry the non-Python files the package relies on.

An editable install masks packaging regressions: importlib.resources resolves
against the source tree, so a build-config change that drops py.typed or the
bundled schemas would pass every other test and break only for wheel installs.
This builds the wheel for real and inspects the archive.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_wheel_ships_marker_and_schemas(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not on PATH")
    # mutmut copies pyproject.toml, src, and tests into mutants/ and runs the
    # suite from there. REPO_ROOT then resolves to that copy, which carries no
    # README.md, so hatchling refuses to build and the nightly sweep fails on a
    # packaging question the copied tree cannot answer.
    if not (REPO_ROOT / "README.md").is_file():
        pytest.skip(f"{REPO_ROOT} is a partial tree, not a checkout")
    build = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert build.returncode == 0, f"uv build failed in {REPO_ROOT}:\n{build.stderr}"
    (wheel,) = tmp_path.glob("rashid-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert "rashid/py.typed" in names
    schemas = [
        n for n in names if n.startswith("rashid/_schemas/portolan/") and n.endswith("schema.json")
    ]
    assert schemas, f"no bundled schema in {wheel.name}"
