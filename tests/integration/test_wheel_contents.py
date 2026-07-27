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
    subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=120,
    )
    (wheel,) = tmp_path.glob("rashid-*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert "rashid/py.typed" in names
    schemas = [
        n for n in names if n.startswith("rashid/_schemas/portolan/") and n.endswith("schema.json")
    ]
    assert schemas, f"no bundled schema in {wheel.name}"
