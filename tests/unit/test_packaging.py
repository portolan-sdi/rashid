"""Packaging invariants: files that must ship inside the installed package."""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_py_typed_marker_is_present() -> None:
    marker = resources.files("rashid_core").joinpath("py.typed")
    assert marker.is_file()


def test_bundled_extension_registry_is_present() -> None:
    """PTL-CNF-004 reads the vendored registry at runtime, so it must ship."""
    registry = resources.files("rashid_core").joinpath("_schemas/extension-registry.json")
    assert registry.is_file()


def test_bundled_profile_schema_is_present() -> None:
    root = resources.files("rashid_core").joinpath("_schemas/portolan")
    versions = [entry.name for entry in root.iterdir() if entry.name.startswith("v")]
    assert versions, "no bundled profile schema under rashid_core/_schemas/portolan"
    for version in versions:
        assert root.joinpath(version, "schema.json").is_file()


def test_core_base_dependency_closure_has_no_geospatial_stack() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    dependencies: dict[str, set[str]] = {}
    for package in lock["package"]:
        dependencies.setdefault(package["name"], set()).update(
            dependency["name"] for dependency in package.get("dependencies", [])
        )
    visited: set[str] = set()
    pending = {"rashid-core"}
    while pending:
        name = pending.pop()
        visited.add(name)
        pending.update(dependencies[name] - visited)
    assert {"click", "jsonschema", "jsonschema-rs", "obstore", "orjson"} <= visited
    assert visited.isdisjoint({"pyarrow", "rasterio", "rio-cogeo", "pyproj", "shapely", "numpy"})


def test_distributions_share_a_version_and_only_meta_defines_the_console_script() -> None:
    core = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    meta = tomllib.loads((ROOT / "meta/rashid/pyproject.toml").read_text(encoding="utf-8"))
    version = core["project"]["version"]
    assert core["project"]["name"] == "rashid-core"
    assert meta["project"]["name"] == "rashid"
    assert meta["project"]["version"] == core["tool"]["commitizen"]["version"] == version
    assert meta["project"]["dependencies"] == [f"rashid-core[data]=={version}"]
    assert "scripts" not in core["project"]
    assert meta["project"]["scripts"] == {"rashid": "rashid_core.cli:main"}
    assert meta["project"]["optional-dependencies"] == {"data": []}
