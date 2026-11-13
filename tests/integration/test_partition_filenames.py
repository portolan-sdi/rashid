"""Partition matches keep their filesystem spelling."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _findings

pytestmark = pytest.mark.integration


@pytest.mark.skipif(sys.platform == "win32", reason="Backslash is a separator on Windows")
def test_posix_partition_filename_keeps_a_literal_backslash(tmp_path: Path) -> None:
    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet"})
    assets.write_geoparquet(directory / "a.parquet")
    path = directory / r"part\bad.parquet"
    assets.write_geoparquet(path, version="1.0.0", columns={"extra": list(range(6))})
    versions = _findings(tmp_path, "PTL-DAT-012")
    assert len(versions) == 1 and path.name in versions[0]["message"]
    schemas = _findings(tmp_path, "PTL-DAT-014")
    assert len(schemas) == 1 and path.name in schemas[0]["message"]


def test_windows_partition_display_uses_posix_separators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import PureWindowsPath

    from rashid.data import _files

    directory = _catalog(tmp_path, **{"partition:glob": "parts/*.parquet"})
    match = r"parts\bad.parquet"
    path = directory / match
    path.parent.mkdir(parents=True, exist_ok=True)
    assets.write_geoparquet(path, version="1.0.0")
    monkeypatch.setattr(_files.globmodule, "glob", lambda *args, **kwargs: [match])
    monkeypatch.setattr(_files, "Path", PureWindowsPath)
    versions = _findings(tmp_path, "PTL-DAT-012")
    assert len(versions) == 1
    assert "partition file 'parts/bad.parquet'" in versions[0]["message"]
    assert "\\" not in versions[0]["message"]
