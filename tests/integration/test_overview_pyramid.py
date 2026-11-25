"""Public CLI regressions for the GeoParquet and file owner track (#266)."""

from __future__ import annotations

import math
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
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"
    # Actual level sizes, not Rasterio's rounded factors (#327).
    sizes = ", ".join(f"{math.ceil(1024 / f)}x{math.ceil(1024 / f)}" for f in factors)
    assert f"overview levels {sizes} " in findings[0]["message"]
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
        assert overview.width == (1 if width == 1 else 3)
        assert overview.height <= 256
    assert not _findings(tmp_path, "PTL-DAT-011")


def test_ordinary_cog_overviews_reuse_the_open_dataset(tmp_path: Path) -> None:
    from rashid_core import _trace

    directory = _catalog(tmp_path)
    path = directory / "ordinary.tif"
    assets.write_cog(path, size=1024, blocksize=256)
    _declare(directory, [path.name])
    with rasterio.open(path) as src:
        assert src.overviews(1) == [2, 4]
    with _trace.counting() as counts:
        assert not _findings(tmp_path, "PTL-DAT-011")
    assert counts["raster.opens"] == 1


def test_square_cog_rounded_overview_factors(tmp_path: Path) -> None:
    """A 32px overview of a 1000px raster is reported as factor 31."""
    from rasterio.shutil import copy as copy_raster

    directory = _catalog(tmp_path)
    source = directory / "source.tif"
    path = directory / "image.tif"
    assets.write_cog(source, size=1000, blocksize=256, overviews=False)
    with rasterio.open(source, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
        dst.build_overviews([2, 4, 8, 16, 32], Resampling.nearest)
    copy_raster(source, path, driver="COG", BLOCKSIZE=256, OVERVIEWS="FORCE_USE_EXISTING")
    _declare(directory, [path.name])
    with rasterio.open(path) as src:
        assert src.overviews(1) == [2, 4, 8, 16, 31]
    assert not _findings(tmp_path, "PTL-DAT-011")


def test_gdal_cog_overviews_rounding_down_pass(tmp_path: Path) -> None:
    """#327: GDAL's COG driver writes 4000x1001 levels as 2000x500 ... 250x62."""
    import numpy as np
    from rasterio.transform import from_bounds

    directory = _catalog(tmp_path)
    path = directory / "odd.tif"
    with rasterio.open(
        path,
        "w",
        driver="COG",
        width=4000,
        height=1001,
        count=1,
        dtype="uint8",
        blocksize=256,
        crs="EPSG:4326",
        transform=from_bounds(*assets.BBOX, 4000, 1001),
    ) as dst:
        dst.write(np.ones((1, 1001, 4000), "uint8"))
    _declare(directory, [path.name])
    with rasterio.open(path, overview_level=3) as overview:
        assert (overview.height, overview.width) == (62, 250)
    assert not _findings(tmp_path, "PTL-DAT-011")
