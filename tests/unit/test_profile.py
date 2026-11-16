"""The bundled profile versions and the spec commit, read from package data."""

from __future__ import annotations

import pytest

from rashid import _profile
from rashid.schema import DEFAULT_SCHEMA_URI, bundled_schema

pytestmark = pytest.mark.unit


def test_versions_sort_numerically() -> None:
    versions = ["v0.10.0", "v0.2.0", "v0.9.1"]
    assert sorted(versions, key=_profile.version_key) == ["v0.2.0", "v0.9.1", "v0.10.0"]


def test_the_default_schema_is_the_newest_bundled_version() -> None:
    newest = _profile.newest_version()
    assert newest == _profile.bundled_versions()[-1]
    assert _profile.profile_uri(newest) == DEFAULT_SCHEMA_URI
    assert bundled_schema(DEFAULT_SCHEMA_URI) is not None


@pytest.mark.parametrize(
    ("uri", "version"),
    [
        ("https://schemas.portolan-sdi.org/portolan/v0.3.0/schema.json", "v0.3.0"),
        ("https://schemas.portolan-sdi.org/portolan/latest/schema.json", None),
        ("https://schemas.portolan-sdi.org/portolan/0.3.0/schema.json", None),
        ("https://example.org/portolan/v0.3.0/schema.json", None),
    ],
)
def test_profile_version_reads_only_a_versioned_profile_uri(uri: str, version: str | None) -> None:
    assert _profile.profile_version(uri) == version


def test_the_spec_commit_matches_spec_ref() -> None:
    from pathlib import Path

    spec_ref = Path(__file__).resolve().parents[2] / "SPEC_REF"
    commit = next(
        line.strip()
        for line in spec_ref.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert _profile.spec_commit() == commit


def test_version_lists_the_bundled_profiles_and_the_spec_commit() -> None:
    from click.testing import CliRunner

    from rashid.cli import main

    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith("rashid, version ")
    versions = _profile.bundled_versions()
    assert lines[1] == f"Portolan profiles: {', '.join(versions)} (applies {versions[-1]})"
    assert lines[2] == f"portolan-spec commit: {_profile.spec_commit()}"
