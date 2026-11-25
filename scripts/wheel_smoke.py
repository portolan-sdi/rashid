"""Build both wheels and prove isolated base installs and upgrades work."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_DISTRIBUTIONS = {
    "rashid-core",
    "click",
    "colorama",
    "jsonschema",
    "jsonschema-rs",
    "jsonschema-specifications",
    "referencing",
    "attrs",
    "rpds-py",
    "typing-extensions",
    "orjson",
    "obstore",
    "tzdata",
    # Python 3.11's venv bootstrap includes setuptools alongside pip.
    "pip",
    "setuptools",
}


def run(command: list[str], *, expected_code: int = 0) -> str:
    print("Run:", " ".join(command), flush=True)
    env = {
        k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    result = subprocess.run(
        command, cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=600
    )
    assert result.returncode == expected_code, f"{command}:\n{result.stdout}\n{result.stderr}"
    return result.stdout


def python_in(path: Path) -> str:
    venv.EnvBuilder(with_pip=True).create(path)
    return str(path / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def check_base(python: str, core: Path) -> None:
    from tests.conftest import CatalogBuilder, default_asset

    run([python, "-I", "-m", "pip", "install", str(core)])
    installed = json.loads(
        run(
            [
                python,
                "-I",
                "-c",
                "import importlib.metadata as m, json; "
                "print(json.dumps(sorted(d.metadata['Name'].lower().replace('_', '-') "
                "for d in m.distributions())))",
            ]
        )
    )
    assert "rashid-core" in installed
    assert set(installed) <= BASE_DISTRIBUTIONS, set(installed) - BASE_DISTRIBUTIONS
    fixture = str(ROOT / "tests/fixtures/reference-catalog")
    run([python, "-I", "-m", "rashid_core", "check", "--no-data", fixture])
    catalog = CatalogBuilder(Path(python).parents[1] / "catalog")
    catalog.collection(
        "broken",
        assets={
            "data": default_asset(),
            "zarr": {
                "href": "./data.zarr",
                "type": "application/vnd.zarr; version=3",
                "roles": ["data"],
                "file:size": 1,
            },
        },
    )
    root = catalog.write()
    (root / "broken/data.parquet").write_bytes(b"wrong size and checksum")
    store = root / "broken/data.zarr"
    store.mkdir()
    (store / ".zgroup").write_text('{"zarr_format": 2}', encoding="utf-8")
    report = json.loads(
        run(
            [python, "-I", "-m", "rashid_core", "check", "--json", str(root)],
            expected_code=1,
        )
    )
    ids = [finding["rule_id"] for finding in report["findings"]]
    assert {"PTL-DAT-000", "PTL-DAT-001", "PTL-DAT-002", "PTL-ZAR-001"} <= set(ids), ids
    assert ids.count("PTL-DAT-000") == 1, report


def check_upgrade(python: str, core: Path, meta: Path, version: str) -> None:
    run([python, "-I", "-m", "pip", "install", "rashid==0.4.0"])
    # Development wheels can still have the old release's version number.
    run(
        [
            python,
            "-I",
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--force-reinstall",
            str(core),
            str(meta),
        ]
    )
    cli = str(Path(python).with_name("rashid.exe" if os.name == "nt" else "rashid"))
    assert run([cli, "--version"]).splitlines()[0] == f"rashid, version {version}"
    run([python, "-I", "-c", "import rashid_core"])


def main() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    with tempfile.TemporaryDirectory(prefix="rashid-wheels-") as directory:
        root = Path(directory)
        wheels = root / "dist"
        run(["uv", "build", "--all-packages", "--wheel", "--out-dir", str(wheels)])
        (core,) = wheels.glob("rashid_core-*.whl")
        (meta,) = wheels.glob("rashid-*.whl")
        with zipfile.ZipFile(core) as archive:
            assert not any(name.endswith("/entry_points.txt") for name in archive.namelist())
        with zipfile.ZipFile(meta) as archive:
            assert all(".dist-info/" in name for name in archive.namelist()), archive.namelist()
        check_base(python_in(root / "base"), core)
        check_upgrade(python_in(root / "upgrade"), core, meta, version)
    print("Both wheels passed isolated install and upgrade checks.")


if __name__ == "__main__":
    main()
