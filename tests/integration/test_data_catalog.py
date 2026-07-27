"""End-to-end data pass over a catalog with real, spec-compliant asset bytes.

Builds a conformant Portolan catalog whose assets are genuine GeoParquet and COG
files — spatially ordered, with a bbox covering column, bounded row groups, and
embedded band statistics — with checksums computed from the bytes at build time,
so nothing is committed and nothing can drift. The pristine catalog passes the
data pass cleanly; each test then mutates one metadata field and asserts the one
finding it should raise. Byte-structure rules (COG validity, spatial ordering,
statistics) are covered in ``test_data_storage``.

Needs the ``rashid[data]`` extra; skips without it. Fully local — no network.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")

from rashid import validate  # noqa: E402
from rashid.catalog import CatalogGraph  # noqa: E402
from rashid.data import (  # noqa: E402
    DAT_CHECKSUM,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_SIZE,
    validate_data,
)
from tests.conftest import CatalogBuilder, mutate_json  # noqa: E402
from tests.integration import _data_assets as assets  # noqa: E402

pytestmark = pytest.mark.integration

_PARQUET_TYPE = "application/vnd.apache.parquet"
_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def _asset(href: str, media_type: str) -> dict[str, Any]:
    return {"href": href, "type": media_type, "roles": ["data"]}


def _patch_checksum(item_json: Path, asset_path: Path) -> None:
    payload = asset_path.read_bytes()
    mutate_json(
        item_json,
        lambda d: d["assets"]["data"].update(
            {"file:size": len(payload), "file:checksum": assets.multihash(payload)}
        ),
    )


def _build(root: Path) -> Path:
    cat = CatalogBuilder(root)
    col = cat.collection("layers")
    col.item("points", assets={"data": _asset("./points.parquet", _PARQUET_TYPE)})
    col.item("raster", assets={"data": _asset("./cog.tif", _COG_TYPE)})
    cat.write()

    layers = root / "layers"
    assets.write_geoparquet(layers / "points" / "points.parquet")
    assets.write_cog(layers / "raster" / "cog.tif")

    _patch_checksum(layers / "points" / "points.json", layers / "points" / "points.parquet")
    _patch_checksum(layers / "raster" / "raster.json", layers / "raster" / "cog.tif")
    return root


@pytest.fixture(scope="module")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("data") / "catalog")


@pytest.fixture
def catalog_root(pristine: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "catalog"
    shutil.copytree(pristine, dst)
    return dst


def _data_findings(root: Path) -> list:
    return validate_data(CatalogGraph.load(root))


def _item(root: Path, name: str) -> Path:
    return root / "layers" / name / f"{name}.json"


def test_pristine_catalog_is_clean(catalog_root: Path) -> None:
    findings = _data_findings(catalog_root)
    assert findings == [], [f"{f.rule_id} {f.message}" for f in findings]


def test_pristine_passes_full_validate(catalog_root: Path) -> None:
    report = validate(catalog_root, data=True)
    assert report.passed
    assert not any(f.rule_id.startswith("PTL-DAT") for f in report.findings)


def test_wrong_checksum_flags_dat_001(catalog_root: Path) -> None:
    empty = "1220" + hashlib.sha256(b"").hexdigest()
    mutate_json(
        _item(catalog_root, "points"),
        lambda d: d["assets"]["data"].__setitem__("file:checksum", empty),
    )
    assert DAT_CHECKSUM in [f.rule_id for f in _data_findings(catalog_root)]


def test_wrong_size_flags_dat_002(catalog_root: Path) -> None:
    mutate_json(
        _item(catalog_root, "points"),
        lambda d: d["assets"]["data"].__setitem__("file:size", 7),
    )
    assert DAT_SIZE in [f.rule_id for f in _data_findings(catalog_root)]


def test_wrong_media_type_flags_dat_003(catalog_root: Path) -> None:
    mutate_json(
        _item(catalog_root, "points"),
        lambda d: d["assets"]["data"].__setitem__("type", "application/vnd.pmtiles"),
    )
    assert DAT_FORMAT in [f.rule_id for f in _data_findings(catalog_root)]


def test_bbox_disagreement_flags_dat_005(catalog_root: Path) -> None:
    mutate_json(
        _item(catalog_root, "points"),
        lambda d: d.__setitem__("bbox", [10.0, 60.0, 12.0, 62.0]),
    )
    dat005 = [f for f in _data_findings(catalog_root) if f.rule_id == DAT_CONSISTENCY]
    assert dat005
    assert dat005[0].severity.value == "warning"


def test_proj_epsg_disagreement_flags_dat_005(catalog_root: Path) -> None:
    mutate_json(
        _item(catalog_root, "points"),
        lambda d: d["assets"]["data"].__setitem__("proj:epsg", 3857),
    )
    assert DAT_CONSISTENCY in [f.rule_id for f in _data_findings(catalog_root)]
