from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    VALID_MULTIHASH,
    CatalogBuilder,
    default_asset,
    findings_for,
    nest_items_under_organizing_catalog,
    thumbnail_asset,
)

pytestmark = pytest.mark.unit


def _asset(**overrides):  # type: ignore[no-untyped-def]
    asset = default_asset()
    for key, value in overrides.items():
        if value is None:
            asset.pop(key, None)
        else:
            asset[key] = value
    return asset


def test_asset_without_roles(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(roles=None)})
    findings = findings_for(validate(catalog.write()), "PTL-AST-001")
    assert len(findings) == 1
    assert "no roles" in findings[0].message


def test_asset_without_type(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(type=None)})
    findings = findings_for(validate(catalog.write()), "PTL-AST-001")
    assert len(findings) == 1
    assert "no type" in findings[0].message


def test_asset_without_href(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(href=None)})
    findings = findings_for(validate(catalog.write()), "PTL-AST-001")
    assert len(findings) == 1
    assert "no href" in findings[0].message


def test_s3_href_is_rejected_with_dedicated_message(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(href="s3://bucket/roads/data.parquet")})
    findings = findings_for(validate(catalog.write()), "PTL-AST-002")
    assert len(findings) == 1
    assert "s3://" in findings[0].message
    assert findings[0].fix_hint is not None


def test_plain_http_href_is_rejected(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(href="http://example.org/data.parquet")})
    findings = findings_for(validate(catalog.write()), "PTL-AST-002")
    assert len(findings) == 1
    assert "'http'" in findings[0].message


def test_https_and_relative_hrefs_pass(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": _asset(href="https://example.org/data.parquet"),
            "local": _asset(href="./data.parquet"),
        },
    )
    assert findings_for(validate(catalog.write()), "PTL-AST-002") == []


def test_missing_file_size(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(**{"file:size": None})})
    findings = findings_for(validate(catalog.write()), "PTL-AST-003")
    assert len(findings) == 1
    assert "no file:size" in findings[0].message
    # PORTO-CORE-028 is a SHOULD, so the absence never fails a catalog.
    assert findings[0].severity is Severity.WARNING


def test_negative_file_size(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(**{"file:size": -5})})
    findings = findings_for(validate(catalog.write()), "PTL-AST-003")
    assert len(findings) == 1
    assert "positive integer" in findings[0].message


def test_missing_checksum(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(**{"file:checksum": None})})
    report = validate(catalog.write())
    findings = findings_for(report, "PTL-AST-003")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    # A mirror that cannot checksum bytes it does not host still conforms.
    assert report.passed
    # AST-004 does not double-report the absence
    assert findings_for(report, "PTL-AST-004") == []


def test_raw_sha256_checksum_is_rejected(catalog: CatalogBuilder) -> None:
    raw_digest = VALID_MULTIHASH[4:]
    catalog.collection("roads", assets={"data": _asset(**{"file:checksum": raw_digest})})
    findings = findings_for(validate(catalog.write()), "PTL-AST-004")
    assert len(findings) == 1
    assert "multihash" in findings[0].message


def test_item_assets_template_checks_type_and_roles_only(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        item_assets={
            "data": {"type": "application/vnd.apache.parquet", "roles": ["data"]},
            "broken": {"type": "application/vnd.apache.parquet"},
        },
    )
    report = validate(catalog.write())
    findings = findings_for(report, "PTL-AST-001")
    assert len(findings) == 1
    assert "'broken'" in findings[0].message
    # templates are exempt from href/size/checksum requirements
    assert all(
        "data" not in f.json_pointer
        for f in findings_for(report, "PTL-AST-003")
        if f.json_pointer and "item_assets" in f.json_pointer
    )


def test_item_asset_defaults_pass(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("roads-2024")
    report = validate(catalog.write())
    for rule in ("PTL-AST-001", "PTL-AST-002", "PTL-AST-003", "PTL-AST-004"):
        assert findings_for(report, rule) == []


def test_catalog_with_assets_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    catalog.overrides["assets"] = {"data": default_asset()}
    findings = findings_for(validate(catalog.write()), "PTL-AST-005")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/assets"
    assert findings[0].path == "catalog.json"


def test_subcatalog_with_assets_is_flagged(catalog: CatalogBuilder) -> None:
    sub = catalog.subcatalog("regions")
    sub.collection("roads")
    sub.overrides["assets"] = {}  # even an empty assets field is the wrong level
    findings = findings_for(validate(catalog.write()), "PTL-AST-005")
    assert len(findings) == 1
    assert findings[0].path == "regions/catalog.json"


def test_catalog_without_assets_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    assert findings_for(validate(catalog.write()), "PTL-AST-005") == []


def test_plain_geotiff_data_asset_needs_cog_media_type(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "scenes",
        assets={
            "data": _asset(
                href="./scene.tif",
                type="image/tiff; application=geotiff",
                roles=["data"],
            )
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-AST-006")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].json_pointer == "/assets/data/type"
    assert findings[0].expected == ("image/tiff; application=geotiff; profile=cloud-optimized")
    assert findings[0].actual == "image/tiff; application=geotiff"


def test_tiff_href_reveals_wrong_media_type(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "scenes",
        assets={
            "data": _asset(
                href="https://example.org/scene.TIFF?download=1",
                type="application/octet-stream",
                roles=["data"],
            )
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-AST-006")
    assert len(findings) == 1
    assert findings[0].actual == "application/octet-stream"


def test_canonical_cog_media_type_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "scenes",
        assets={
            "data": _asset(
                href="./scene.tif",
                type="image/tiff; application=geotiff; profile=cloud-optimized",
                roles=["data"],
            )
        },
    )
    assert findings_for(validate(catalog.write()), "PTL-AST-006") == []


def test_upstream_source_geotiff_is_exempt(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "scenes",
        assets={
            "source": _asset(
                href="https://example.org/original.tif",
                type="image/tiff; application=geotiff",
                roles=["data", "source"],
            )
        },
    )
    assert findings_for(validate(catalog.write()), "PTL-AST-006") == []


def test_item_asset_template_needs_cog_media_type(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "scenes",
        item_assets={
            "data": {
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
            }
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-AST-006")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/item_assets/data/type"


def test_collection_with_only_a_thumbnail_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"thumbnail": thumbnail_asset()})
    findings = findings_for(validate(catalog.write()), "PTL-AST-007")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "roads/collection.json"
    assert findings[0].json_pointer == "/assets"


def test_collection_without_assets_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={})
    assert len(findings_for(validate(catalog.write()), "PTL-AST-007")) == 1


def test_collection_data_on_its_items_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("scenes", assets={"thumbnail": thumbnail_asset()}).item("a")
    assert findings_for(validate(catalog.write()), "PTL-AST-007") == []


def test_collection_data_on_items_under_an_organizing_catalog_is_clean(
    catalog: CatalogBuilder,
) -> None:
    catalog.collection("scenes", assets={"thumbnail": thumbnail_asset()}).item("a")
    root = catalog.write()
    nest_items_under_organizing_catalog(root, root / "scenes")
    assert findings_for(validate(root), "PTL-AST-007") == []


def test_partition_glob_refers_to_the_data(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "parts",
        assets={"thumbnail": thumbnail_asset()},
        **{"partition:glob": "./**/*.parquet"},
    )
    assert findings_for(validate(catalog.write()), "PTL-AST-007") == []


def test_blank_partition_glob_does_not_refer_to_the_data(catalog: CatalogBuilder) -> None:
    catalog.collection("parts", assets={"thumbnail": thumbnail_asset()}, **{"partition:glob": " "})
    assert len(findings_for(validate(catalog.write()), "PTL-AST-007")) == 1


def test_data_role_beside_other_roles_counts(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _asset(roles=["data", "source"])}).item(
        "a", assets={"data": _asset(roles=["visual", "data"])}
    )
    report = validate(catalog.write())
    assert findings_for(report, "PTL-AST-007") == []
    assert findings_for(report, "PTL-AST-008") == []


def test_item_without_data_asset_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("scenes").item("a", assets={"thumbnail": thumbnail_asset()})
    report = validate(catalog.write())
    findings = findings_for(report, "PTL-AST-008")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert findings[0].path == "scenes/a/a.json"
    assert findings[0].json_pointer == "/assets"
    # The collection's own data asset still refers to its data.
    assert findings_for(report, "PTL-AST-007") == []


def test_dataless_items_leave_the_collection_without_data(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("scenes", assets={"thumbnail": thumbnail_asset()})
    collection.item("a", assets={"thumbnail": thumbnail_asset()})
    collection.item("b", assets={})
    report = validate(catalog.write())
    assert len(findings_for(report, "PTL-AST-007")) == 1
    assert {f.path for f in findings_for(report, "PTL-AST-008")} == {
        "scenes/a/a.json",
        "scenes/b/b.json",
    }


@pytest.mark.parametrize("scope", ["collection", "item"])
@pytest.mark.parametrize(
    "reference", ["collection", "item", "glob", "none", "other", "raster", "source"]
)
def test_pmtiles_requires_own_collection_data(
    catalog: CatalogBuilder, scope: str, reference: str
) -> None:
    pmtiles = {"href": "./map.pmtiles", "type": "application/vnd.pmtiles", "roles": ["visual"]}
    collection = catalog.collection("roads", assets={})
    if scope == "collection":
        collection.overrides["assets"] = {"map": pmtiles}
    else:
        collection.item("tiles", assets={"map": pmtiles})
    if reference == "collection":
        collection.overrides["assets"]["data"] = default_asset()
    elif reference == "item":
        collection.item("source-data", assets={"data": default_asset()})
    elif reference == "glob":
        collection.overrides["partition:glob"] = "./parts/*.parquet"
    elif reference == "other":
        catalog.collection("other", assets={"data": default_asset()})
    elif reference == "raster":
        collection.overrides["assets"]["data"] = _asset(type="image/tiff", href="./data.tif")
    elif reference == "source":
        collection.overrides["assets"]["data"] = _asset(roles=["source"])
    findings = findings_for(validate(catalog.write()), "PTL-AST-009")
    assert len(findings) == (0 if reference in {"collection", "item", "glob"} else 1)
    for finding in findings:
        assert finding.severity is Severity.ERROR
        assert finding.json_pointer
        assert finding.fix_hint


@pytest.mark.parametrize("roles", [["visual"], ["data"], [], None])
def test_pmtiles_visual_role(catalog: CatalogBuilder, roles: object) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "map": {"href": "./map.pmtiles", "type": "application/vnd.pmtiles", "roles": roles},
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-AST-009")
    assert len(findings) == (0 if roles == ["visual"] else 1)
    if findings:
        assert findings[0].json_pointer == "/assets/map/roles"


@pytest.mark.parametrize("scope", ["collection", "item"])
@pytest.mark.parametrize("media_type", ["application/vnd.pmtiles", "application/octet-stream"])
def test_pmtiles_alone_is_not_primary_data(
    catalog: CatalogBuilder, scope: str, media_type: str
) -> None:
    assets = {
        "map": {"href": "./map.pmtiles?download=1", "type": media_type, "roles": ["data", "visual"]}
    }
    collection = catalog.collection("roads", assets=assets if scope == "collection" else {})
    if scope == "item":
        collection.item("tiles", assets=assets)
    report = validate(catalog.write())
    assert len(findings_for(report, "PTL-AST-007")) == 1
    assert len(findings_for(report, "PTL-AST-009")) == 1
    if scope == "item":
        assert len(findings_for(report, "PTL-AST-008")) == 1
