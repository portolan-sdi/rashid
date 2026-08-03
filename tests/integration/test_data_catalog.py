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
from tests.conftest import CatalogBuilder, mutate_json, thumbnail_asset  # noqa: E402
from tests.integration import _data_assets as assets  # noqa: E402

pytestmark = pytest.mark.integration

_PARQUET_TYPE = "application/vnd.apache.parquet"
_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"


def _asset(href: str, media_type: str) -> dict[str, Any]:
    return {"href": href, "type": media_type, "roles": ["data"]}


def _patch_checksum(item_json: Path, asset_path: Path, key: str = "data") -> None:
    payload = asset_path.read_bytes()
    mutate_json(
        item_json,
        lambda d: d["assets"][key].update(
            {"file:size": len(payload), "file:checksum": assets.multihash(payload)}
        ),
    )


def _build(root: Path) -> Path:
    cat = CatalogBuilder(root)
    # The collection carries two real assets of its own: a proj:epsg written at
    # the collection root governs both, which is what PTL-DAT-005 must report
    # once rather than once per inheritor.
    col = cat.collection(
        "layers",
        assets={
            "data": _asset("./layers.parquet", _PARQUET_TYPE),
            "extra": _asset("./extra.parquet", _PARQUET_TYPE),
            "thumbnail": thumbnail_asset(),
        },
        # PORTO-FMT-044: a vector collection documents its columns. Declared at
        # collection level, which covers both of its GeoParquet data assets.
        **{
            "table:columns": [
                {"name": "geometry", "type": "binary", "description": "Point geometry as WKB."}
            ]
        },
    )
    # PORTO-FMT-045 puts an item's columns in properties, where the table
    # extension scopes the field for items.
    col.item(
        "points",
        assets={"data": _asset("./points.parquet", _PARQUET_TYPE)},
        properties={
            "datetime": "2024-01-01T00:00:00Z",
            "table:columns": [
                {"name": "geometry", "type": "binary", "description": "Point geometry as WKB."}
            ],
        },
    )
    col.item("raster", assets={"data": _asset("./cog.tif", _COG_TYPE)})
    cat.write()

    layers = root / "layers"
    assets.write_geoparquet(layers / "layers.parquet")
    assets.write_geoparquet(layers / "extra.parquet")
    assets.write_geoparquet(layers / "points" / "points.parquet")
    assets.write_cog(layers / "raster" / "cog.tif")

    _patch_checksum(layers / "collection.json", layers / "layers.parquet")
    _patch_checksum(layers / "collection.json", layers / "extra.parquet", key="extra")
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
    (finding,) = [f for f in _data_findings(catalog_root) if f.rule_id == DAT_SIZE]
    # expected carries the true byte count, actual the declared value: enough
    # for a caller to repair the metadata without re-reading the asset.
    assert finding.actual == 7
    assert isinstance(finding.expected, int) and finding.expected != 7


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
    (finding,) = [f for f in _data_findings(catalog_root) if f.rule_id == DAT_CONSISTENCY]
    # The asset declares the value itself, so the asset is what disagrees with
    # its own bytes and the finding stays on the asset.
    assert finding.json_pointer == "/assets/data"
    assert finding.message.startswith("asset 'data' declares proj:epsg 3857")


def _collection(root: Path) -> Path:
    return root / "layers" / "collection.json"


def test_collection_root_proj_epsg_is_reported_once_at_the_declaration(
    catalog_root: Path,
) -> None:
    mutate_json(_collection(catalog_root), lambda d: d.__setitem__("proj:epsg", 3857))
    findings = [f for f in _data_findings(catalog_root) if f.rule_id == DAT_CONSISTENCY]
    # Both collection assets inherit the one root declaration; one field is one
    # finding, and it points at the field a reader has to edit.
    (finding,) = findings
    assert finding.path == "layers/collection.json"
    assert finding.json_pointer == "/proj:epsg"
    assert finding.message == (
        "collection 'layers' declares proj:epsg 3857 at its root, which its assets "
        "inherit, but the asset data is EPSG:4326"
    )
    # The bytes settle the disagreement, so a rewriter has both values it needs.
    assert finding.expected == 4326
    assert finding.actual == 3857


def test_collection_root_proj_epsg_agreeing_with_the_data_is_clean(catalog_root: Path) -> None:
    mutate_json(_collection(catalog_root), lambda d: d.__setitem__("proj:epsg", 4326))
    findings = _data_findings(catalog_root)
    assert findings == [], [f"{f.rule_id} {f.message}" for f in findings]


def test_item_properties_proj_epsg_points_at_the_properties(catalog_root: Path) -> None:
    mutate_json(
        _item(catalog_root, "points"),
        lambda d: d["properties"].__setitem__("proj:epsg", 3857),
    )
    (finding,) = [f for f in _data_findings(catalog_root) if f.rule_id == DAT_CONSISTENCY]
    assert finding.json_pointer == "/properties/proj:epsg"
    assert "item 'points' declares proj:epsg 3857 in its properties" in finding.message
