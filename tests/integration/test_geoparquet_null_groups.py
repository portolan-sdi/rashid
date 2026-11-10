"""Public CLI regressions for the GeoParquet and file owner track (#266)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare, _findings

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("zero_ordering_size_floor_fixture")]


@pytest.mark.parametrize("null_group", [0, 2, 5])
def test_null_only_group_retains_statistics_and_ordering(tmp_path: Path, null_group: int) -> None:
    """#197: null-only groups have no spatial position and are prunable."""
    directory = _catalog(tmp_path)
    path = directory / "nulls.parquet"
    points = assets.interleaved_points()
    offset = null_group * 2
    points[offset:offset] = [(0.0, 0.0), (0.0, 0.0)]
    assets.write_geoparquet(path, points=points, null_rows={offset, offset + 1})
    _declare(directory, [path.name])
    assert not _findings(tmp_path, "PTL-DAT-007")
    assert [f["severity"] for f in _findings(tmp_path, "PTL-DAT-006")] == ["error"]
    assets.write_geoparquet(path, points=points, null_rows=set(range(len(points))))
    assert [f["severity"] for f in _findings(tmp_path, "PTL-DAT-007")] == ["error"]
