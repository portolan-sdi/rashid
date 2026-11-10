from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    VALID_MULTIHASH,
    CatalogBuilder,
    default_asset,
    findings_for,
    mutate_json,
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


@pytest.mark.parametrize("scope", ["collection", "item"])
@pytest.mark.parametrize("location", ["relative", "absolute", "root-self", "object-self", "other"])
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"file:size": 1},
        {"file:checksum": VALID_MULTIHASH},
        {"file:size": None, "file:checksum": None},
    ],
)
def test_asset_file_fields_for_self_reference(
    catalog: CatalogBuilder, scope: str, location: str, fields: dict
) -> None:
    collection = catalog.collection("roads", assets={})
    path = "roads/collection.json"
    relative = "./sub/../collection.json"
    if scope == "item":
        collection.item("seg1", assets={})
        path = "roads/seg1/seg1.json"
        relative = "../seg1/./seg1.json"
    root = catalog.write()
    href = relative
    if location == "absolute":
        href = str(root / path)
    elif location in {"root-self", "object-self"}:
        href = f"https://example.org/catalog/{path}"
        self_path = root / ("catalog.json" if location == "root-self" else path)
        self_href = "https://example.org/catalog/catalog.json" if location == "root-self" else href
        mutate_json(self_path, lambda d: d["links"].append({"rel": "self", "href": self_href}))
    elif location == "other":
        href = "../other/collection.json"
    mutate_json(
        root / path,
        lambda d: d["assets"].update(
            {"object": {"href": href, "type": "application/json", "roles": ["metadata"], **fields}}
        ),
    )
    findings = [f for f in findings_for(validate(root), "PTL-AST-003") if f.path == path]
    if location == "other":
        assert all(f.severity is Severity.WARNING for f in findings)
        assert len(findings) == sum(
            fields.get(field) is None for field in ("file:size", "file:checksum")
        )
    else:
        assert len(findings) == len(fields)
        assert all(f.severity is Severity.ERROR for f in findings)
        assert {f.json_pointer for f in findings} == {f"/assets/object/{field}" for field in fields}
        assert all(f.fix_hint for f in findings)


@pytest.mark.parametrize("scheme,port", [("https", 443), ("http", 80)])
def test_self_reference_default_port(catalog: CatalogBuilder, scheme: str, port: int) -> None:
    url = f"{scheme}://example.org/roads/collection.json"
    catalog.collection(
        "roads",
        links=[{"rel": "self", "href": url}],
        assets={"self": _asset(href=f"{scheme}://example.org:{port}/roads/collection.json")},
    )
    assert len(findings_for(validate(catalog.write()), "PTL-AST-003")) == 2


@pytest.mark.parametrize(
    "encoded,plain,equal",
    [
        ("roads%2Fcollection.json", "roads/collection.json", False),
        ("roads%3Bv/collection.json", "roads;v/collection.json", True),
        ("roads%20v/collection.json", "roads v/collection.json", True),
        ("roads%3Bv/collection.json?x=1", "roads;v/collection.json?x=1", False),
    ],
)
def test_self_reference_follows_transport_identity(
    catalog: CatalogBuilder, encoded: str, plain: str, equal: bool
) -> None:
    catalog.collection(
        "roads",
        links=[{"rel": "self", "href": f"https://example.org/{plain}"}],
        assets={"separate": _asset(href=f"https://example.org/{encoded}")},
    )
    findings = findings_for(validate(catalog.write()), "PTL-AST-003")
    assert len(findings) == (2 if equal else 0)
    assert all(f.severity is Severity.ERROR for f in findings)


@pytest.mark.parametrize(
    "directory,encoded", [("sector 1", "sector%201"), ("東京", "%E6%9D%B1%E4%BA%AC")]
)
@pytest.mark.parametrize("prefix", ["catalog", "catalog%2Fv"])
def test_published_self_reference_under_encoded_directory(
    catalog: CatalogBuilder, directory: str, encoded: str, prefix: str
) -> None:
    catalog.overrides["links"] = [
        {"rel": "self", "href": f"https://example.org/{prefix}/catalog.json"}
    ]
    catalog.subcatalog(directory).collection(
        "roads",
        assets={
            "self": _asset(href=f"https://example.org/{prefix}/{encoded}/roads/collection.json")
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-AST-003")
    assert len(findings) == 2
    assert all(f.severity is Severity.ERROR for f in findings)


@pytest.mark.parametrize("scope", ["asset", "collection", "item", "glob"])
@pytest.mark.parametrize(
    "columns,expected",
    [
        ([{"name": "population", "type": "int64"}], Severity.ERROR),
        ([{"name": "geometry", "type": "binary"}], None),
        (None, None),
    ],
)
def test_pmtiles_reference_geometry(
    catalog: CatalogBuilder, scope: str, columns: object, expected: object
) -> None:
    data = default_asset()
    collection = catalog.collection(
        "roads", assets={"map": {"href": "./map.pmtiles", "roles": ["visual"]}}
    )
    if scope == "glob":
        collection.overrides["partition:glob"] = "./parts/*.parquet"
        if columns is not None:
            collection.overrides["table:columns"] = columns
    elif scope == "item":
        properties = {} if columns is None else {"table:columns": columns}
        collection.item("data", assets={"data": data}, properties=properties)
    else:
        collection.overrides["assets"]["data"] = data
        if columns is not None:
            (data if scope == "asset" else collection.overrides)["table:columns"] = columns
    findings = findings_for(validate(catalog.write()), "PTL-AST-009")
    assert [f.severity for f in findings] == ([] if expected is None else [expected])


@pytest.mark.parametrize(
    "scope", ["asset", "collection", "properties", "item", "item-asset", "item-collection", "glob"]
)
@pytest.mark.parametrize("with_columns", [False, True])
def test_pmtiles_reference_primary_geometry(
    catalog: CatalogBuilder, scope: str, with_columns: bool
) -> None:
    declaration = {"table:primary_geometry": "footprint"}
    if with_columns:
        declaration["table:columns"] = [{"name": "footprint", "type": "binary"}]
    data = default_asset()
    collection = catalog.collection(
        "roads", assets={"map": {"href": "./map.pmtiles", "roles": ["visual"]}}
    )
    if scope == "glob":
        collection.overrides.update({"partition:glob": "./parts/*.parquet", **declaration})
    elif scope in {"item", "item-asset", "item-collection"}:
        item = collection.item("part", assets={"data": data})
        if scope == "item-asset":
            data.update(declaration)
        elif scope == "item-collection":
            collection.overrides.update(declaration)
        else:
            item.overrides["properties"] = declaration
    else:
        collection.overrides["assets"]["data"] = data
        if scope == "asset":
            data.update(declaration)
        elif scope == "properties":
            collection.overrides["properties"] = declaration
        else:
            collection.overrides.update(declaration)
    assert findings_for(validate(catalog.write()), "PTL-AST-009") == []


@pytest.mark.parametrize("role", ["source", "metadata"])
def test_pmtiles_reference_ignores_other_assets_primary_geometry(
    catalog: CatalogBuilder, role: str
) -> None:
    catalog.collection(
        "roads",
        **{"table:columns": [{"name": "population", "type": "int64"}]},
        assets={
            "data": default_asset(),
            "other": _asset(roles=[role], **{"table:primary_geometry": "footprint"}),
            "map": {"href": "./map.pmtiles", "roles": ["visual"]},
        },
    )
    assert len(findings_for(validate(catalog.write()), "PTL-AST-009")) == 1


@pytest.mark.parametrize("reference", ["missing", "unknown", "spatial", "nonspatial"])
def test_pmtiles_reference_scanned_once(catalog: CatalogBuilder, reference: str) -> None:
    from unittest.mock import patch

    from rashid.rules import assets

    collection = catalog.collection("roads", assets={})
    if reference != "missing":
        collection.overrides["assets"]["data"] = default_asset()
    if reference == "spatial":
        collection.overrides["table:columns"] = [{"name": "geometry", "type": "binary"}]
    elif reference == "nonspatial":
        collection.overrides["table:columns"] = [{"name": "population", "type": "int64"}]
    for i in range(10):
        collection.item(str(i), assets={"map": {"href": "./map.pmtiles", "roles": ["visual"]}})
    with patch.object(
        assets, "_has_geoparquet_reference", wraps=assets._has_geoparquet_reference
    ) as scan:
        root = catalog.write()
        first = findings_for(validate(root), "PTL-AST-009")
        assert scan.call_count == 1
        mutate_json(root / "roads/collection.json", lambda data: data.__setitem__("assets", {}))
        second = findings_for(validate(root), "PTL-AST-009")
    assert scan.call_count == 2
    assert len(first) == (10 if reference in {"missing", "nonspatial"} else 0)
    assert len(second) == 10
    assert all(f.severity is Severity.ERROR for f in second)


@pytest.mark.parametrize("spatial_scope", ["asset", "collection"])
def test_pmtiles_reference_combines_column_declarations(
    catalog: CatalogBuilder, spatial_scope: str
) -> None:
    spatial = [{"name": "geometry", "type": "binary"}]
    tabular = [{"name": "population", "type": "int64"}]
    data = _asset(**{"table:columns": spatial if spatial_scope == "asset" else tabular})
    catalog.collection(
        "roads",
        **{"table:columns": spatial if spatial_scope == "collection" else tabular},
        assets={"data": data, "map": {"href": "./map.pmtiles", "roles": ["visual"]}},
    )
    assert findings_for(validate(catalog.write()), "PTL-AST-009") == []


@pytest.mark.parametrize("organized", [False, True])
def test_pmtiles_item_findings_name_the_item(catalog: CatalogBuilder, organized: bool) -> None:
    collection = catalog.collection("roads", assets={})
    collection.item("tiles", assets={"map": {"href": "./map.pmtiles", "roles": ["data"]}})
    root = catalog.write()
    if organized:
        nest_items_under_organizing_catalog(root, root / "roads")
    findings = findings_for(validate(root), "PTL-AST-009")
    assert len(findings) == 2
    assert {f.path for f in findings} == {
        "roads/2024/tiles/tiles.json" if organized else "roads/tiles/tiles.json"
    }
    assert {f.object_id for f in findings} == {"tiles"}
    assert {f.json_pointer for f in findings} == {"/assets/map", "/assets/map/roles"}
    assert all(f.severity is Severity.ERROR for f in findings)


def test_orphan_pmtiles_item_still_reports_errors(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pathlib import PurePosixPath

    from rashid.catalog import CatalogGraph, Node
    from rashid.rules.assets import PMTilesDataRule

    node = Node(
        PurePosixPath("tiles.json"),
        tmp_path / "tiles.json",
        "item",
        "tiles",
        {"assets": {"map": {"href": "./map.pmtiles", "roles": ["data"]}}},
    )
    graph = CatalogGraph(tmp_path, nodes={node.path: node})
    findings = list(PMTilesDataRule().check(node, graph))
    assert len(findings) == 2
    assert all(f.path == "tiles.json" and f.severity is Severity.ERROR for f in findings)
