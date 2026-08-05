from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    VALID_MULTIHASH,
    CatalogBuilder,
    default_asset,
    findings_for,
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
