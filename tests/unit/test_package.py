"""Package resource and distribution lookup behavior."""

from importlib import metadata

import pytest

from rashid_core import _package

pytestmark = pytest.mark.unit


def test_schemas_resolves_bundled_data() -> None:
    assert _package.schemas().joinpath("report/v1.0.0/schema.json").is_file()


def test_version_matches_the_installed_distribution() -> None:
    assert _package.version() == metadata.version("rashid-core")


def test_version_without_distribution_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", missing)
    assert _package.version() == "unknown"
