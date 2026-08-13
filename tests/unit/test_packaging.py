"""Packaging invariants: files that must ship inside the installed package."""

from __future__ import annotations

from importlib import resources

import pytest

pytestmark = pytest.mark.unit


def test_py_typed_marker_is_present() -> None:
    marker = resources.files("rashid").joinpath("py.typed")
    assert marker.is_file()


def test_bundled_extension_registry_is_present() -> None:
    """PTL-CNF-004 reads the vendored registry at runtime, so it must ship."""
    registry = resources.files("rashid").joinpath("_schemas/extension-registry.json")
    assert registry.is_file()


def test_bundled_profile_schema_is_present() -> None:
    root = resources.files("rashid").joinpath("_schemas/portolan")
    versions = [entry.name for entry in root.iterdir() if entry.name.startswith("v")]
    assert versions, "no bundled profile schema under rashid/_schemas/portolan"
    for version in versions:
        assert root.joinpath(version, "schema.json").is_file()
