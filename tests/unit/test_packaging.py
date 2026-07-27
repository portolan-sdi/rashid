"""Packaging invariants: files that must ship inside the installed package."""

from __future__ import annotations

from importlib import resources

import pytest

pytestmark = pytest.mark.unit


def test_py_typed_marker_is_present() -> None:
    marker = resources.files("rashid").joinpath("py.typed")
    assert marker.is_file()
