"""Public CLI regressions for the GeoParquet and file owner track (#266)."""

from __future__ import annotations

from pathlib import Path

import pytest
import rasterio
from rasterio.enums import Resampling

from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare, _findings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("factors", [[2], [16], [2, 32], [3, 4]])
def test_overview_pyramid_depth_and_reductions(tmp_path: Path, factors: list[int]) -> None:
    """#204: presence alone does not prove the pyramid reaches a tile at valid steps."""
    directory = _catalog(tmp_path)
    path = directory / "image.tif"
    assets.write_cog(path, size=1024, blocksize=256, overviews=False)
    with rasterio.open(path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.build_overviews(factors, Resampling.nearest)
    _declare(directory, [path.name])
    findings = _findings(tmp_path, "PTL-DAT-011")
    assert len(findings) == 1 and findings[0]["severity"] == "error"
    assets.write_cog(path, size=1024, blocksize=256)
    assert not _findings(tmp_path, "PTL-DAT-011")


@pytest.mark.parametrize("width", [1, 21])
@pytest.mark.parametrize("factors", [[2, 4, 8], [2, 4, 8, 16], [4, 8], [4, 10]])
def test_narrow_raster_uses_actual_overview_dimensions(
    tmp_path: Path, factors: list[int], width: int
) -> None:
    """21x2048 at nominal 8x is 3x256, despite Rasterio reporting factor 7."""
    from rasterio.transform import from_bounds

    directory = _catalog(tmp_path)
    path = directory / "narrow.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=2048,
        count=1,
        dtype="uint8",
        tiled=True,
        blockxsize=256,
        blockysize=256,
        crs="EPSG:4326",
        transform=from_bounds(*assets.BBOX, width, 2048),
    ) as dst:
        dst.build_overviews(factors, Resampling.nearest)
    _declare(directory, [path.name])
    with rasterio.open(
        path, overview_level=factors.index(8) if 8 in factors else len(factors) - 1
    ) as overview:
        assert overview.width == (1 if width == 1 else 3) and overview.height <= 256
    assert not _findings(tmp_path, "PTL-DAT-011")


def test_ordinary_cog_overviews_reuse_the_open_dataset(tmp_path: Path) -> None:
    from rashid import _trace

    directory = _catalog(tmp_path)
    path = directory / "ordinary.tif"
    assets.write_cog(path, size=1024, blocksize=256)
    _declare(directory, [path.name])
    with rasterio.open(path) as src:
        assert src.overviews(1) == [2, 4]
    with _trace.counting() as counts:
        assert not _findings(tmp_path, "PTL-DAT-011")
    assert counts["raster.opens"] == 1
