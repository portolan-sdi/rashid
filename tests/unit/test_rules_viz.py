from __future__ import annotations

import pytest

from rashid import validate
from rashid.model import Severity
from tests.conftest import (
    CatalogBuilder,
    default_asset,
    findings_for,
    mutate_json,
    thumbnail_asset,
)

pytestmark = pytest.mark.unit

_PMTILES_LINK = {
    "rel": "pmtiles",
    "href": "./data.pmtiles",
    "type": "application/vnd.pmtiles",
    "pmtiles:layers": ["data"],
}
_WEB_MAP_LINKS_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"


def _pmtiles_asset() -> dict:
    asset = default_asset()
    asset["href"] = "./data.pmtiles"
    asset["type"] = "application/vnd.pmtiles"
    asset["roles"] = ["visual"]
    return asset


def _style_asset() -> dict:
    asset = default_asset()
    asset["href"] = "./styles/default.json"
    asset["type"] = "application/vnd.mapbox.style+json"
    asset["roles"] = ["style"]
    return asset


def test_geospatial_collection_without_thumbnail(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", assets={"data": default_asset()})
    collection.item("roads-2024")  # item geometry marks the collection geospatial
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1
    assert "thumbnail" in findings[0].message


def test_thumbnail_with_wrong_type(catalog: CatalogBuilder) -> None:
    bad = thumbnail_asset()
    bad["type"] = "image/gif"
    collection = catalog.collection("roads", assets={"data": default_asset(), "thumbnail": bad})
    collection.item("roads-2024")
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1
    assert "image/gif" in findings[0].message


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg", "image/webp"])
def test_thumbnail_accepts_each_allowed_type(catalog: CatalogBuilder, media_type: str) -> None:
    asset = thumbnail_asset()
    asset["type"] = media_type
    collection = catalog.collection("roads", assets={"data": default_asset(), "thumbnail": asset})
    collection.item("roads-2024")
    assert findings_for(validate(catalog.write()), "PTL-VIZ-001") == []


def test_undecidable_collection_without_thumbnail_warns(catalog: CatalogBuilder) -> None:
    # single parquet asset, no items, no table:columns: could be tabular
    catalog.collection("prices", assets={"data": default_asset()})
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "cannot be decided from metadata" in findings[0].message
    assert "it has no items" in findings[0].message
    assert "table:columns" in findings[0].message
    assert "PMTiles, COG, or COPC" in findings[0].message
    assert findings[0].json_pointer == "/assets"
    assert "table:columns" in (findings[0].fix_hint or "")


@pytest.mark.parametrize(
    ("count", "clause"),
    [(1, "its one item declares no geometry"), (2, "none of its 2 items declares a geometry")],
)
def test_undecidable_message_names_geometryless_items(
    catalog: CatalogBuilder, count: int, clause: str
) -> None:
    collection = catalog.collection("prices", assets={"data": default_asset()})
    for index in range(count):
        collection.item(f"prices-202{index}")
    root = catalog.write()
    for index in range(count):
        item_id = f"prices-202{index}"
        mutate_json(
            root / "prices" / item_id / f"{item_id}.json",
            lambda d: (d.__setitem__("geometry", None), d.pop("bbox")),
        )
    findings = findings_for(validate(root), "PTL-VIZ-001")
    assert len(findings) == 1
    assert clause in findings[0].message


def test_undecidable_collection_with_thumbnail_is_silent(catalog: CatalogBuilder) -> None:
    """The requirement is met whichever way the question would have resolved."""
    catalog.collection("prices", assets={"data": default_asset(), "thumbnail": thumbnail_asset()})
    assert findings_for(validate(catalog.write()), "PTL-VIZ-001") == []


def test_asset_columns_without_geometry_mark_tabular(catalog: CatalogBuilder) -> None:
    """The spec's reference catalog declares table:columns on the data asset."""
    asset = default_asset()
    asset["table:columns"] = [{"name": "year", "type": "int64"}]
    catalog.collection("prices", assets={"data": asset})
    assert findings_for(validate(catalog.write()), "PTL-VIZ-001") == []


def test_asset_geometry_column_marks_geospatial(catalog: CatalogBuilder) -> None:
    asset = default_asset()
    asset["table:columns"] = [{"name": "geom", "type": "binary"}]
    catalog.collection("places", assets={"data": asset})
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_source_asset_columns_do_not_decide(catalog: CatalogBuilder) -> None:
    """An upstream CSV's columns describe what was converted, not what ships."""
    source = default_asset()
    source["href"] = "./prices.csv"
    source["type"] = "text/csv"
    source["roles"] = ["source"]
    source["table:columns"] = [{"name": "year", "type": "int64"}]
    catalog.collection("places", assets={"data": default_asset(), "source": source})
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING


def test_spatial_media_type_outranks_a_geometryless_column_list(catalog: CatalogBuilder) -> None:
    """A COG stays raster however an attribute table beside it is described."""
    cog = default_asset()
    cog["href"] = "./scene.tif"
    cog["type"] = "image/tiff; application=geotiff; profile=cloud-optimized"
    table = default_asset()
    table["table:columns"] = [{"name": "year", "type": "int64"}]
    catalog.collection("scenes", assets={"data": cog, "table": table})
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR


def test_declared_columns_without_geometry_mark_tabular(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "prices",
        assets={"data": default_asset()},
        **{"table:columns": [{"name": "year", "type": "int64"}]},
    )
    assert findings_for(validate(catalog.write()), "PTL-VIZ-001") == []


def test_geometry_column_marks_geospatial(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "places",
        assets={"data": default_asset()},
        **{"table:columns": [{"name": "geometry", "type": "binary"}]},
    )
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-001")
    assert len(findings) == 1


def test_visual_asset_without_style(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "tiles": _pmtiles_asset(),
        },
    )
    collection.item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: (
            d["links"].append(dict(_PMTILES_LINK)),
            d["stac_extensions"].append(_WEB_MAP_LINKS_URI),
        ),
    )
    findings = findings_for(validate(root), "PTL-VIZ-002")
    assert len(findings) == 1
    assert "'style'" in findings[0].message


def test_visual_asset_with_style_passes(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "tiles": _pmtiles_asset(),
            "default-style": _style_asset(),
        },
    )
    collection.item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: (
            d["links"].append(dict(_PMTILES_LINK)),
            d["stac_extensions"].append(_WEB_MAP_LINKS_URI),
        ),
    )
    report = validate(root)
    assert findings_for(report, "PTL-VIZ-002") == []
    assert findings_for(report, "PTL-VIZ-003") == []


def test_pmtiles_asset_without_link(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "tiles": _pmtiles_asset(),
            "default-style": _style_asset(),
        },
    )
    collection.item("roads-2024")
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-003")
    assert len(findings) == 1
    assert "rel:'pmtiles'" in findings[0].message


def test_pmtiles_link_without_layers_or_extension(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads", assets={"data": default_asset()})
    collection.item("roads-2024")
    root = catalog.write()
    incomplete = {"rel": "pmtiles", "href": "./data.pmtiles", "type": "application/vnd.pmtiles"}
    mutate_json(root / "roads" / "collection.json", lambda d: d["links"].append(incomplete))
    messages = [f.message for f in findings_for(validate(root), "PTL-VIZ-003")]
    assert len(messages) == 2
    assert any("pmtiles:layers" in m for m in messages)
    assert any("web-map-links" in m for m in messages)


def test_large_vector_without_visual_is_info(catalog: CatalogBuilder) -> None:
    big = default_asset()
    big["file:size"] = 553_395_618
    collection = catalog.collection("places", assets={"data": big, "thumbnail": thumbnail_asset()})
    collection.item("places-2026")
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-004")
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO


def test_large_undecidable_asset_without_visual_reports_the_uncertainty(
    catalog: CatalogBuilder,
) -> None:
    big = default_asset()
    big["file:size"] = 553_395_618
    catalog.collection("prices", assets={"data": big, "thumbnail": thumbnail_asset()})
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-004")
    assert len(findings) == 1
    assert findings[0].severity is Severity.INFO
    assert "cannot be decided from metadata" in findings[0].message
    assert "553395618 bytes" in findings[0].message
    assert "PMTiles" in (findings[0].fix_hint or "")


def test_large_tabular_asset_without_visual_is_silent(catalog: CatalogBuilder) -> None:
    big = default_asset()
    big["file:size"] = 553_395_618
    big["table:columns"] = [{"name": "year", "type": "int64"}]
    catalog.collection("prices", assets={"data": big, "thumbnail": thumbnail_asset()})
    assert findings_for(validate(catalog.write()), "PTL-VIZ-004") == []


def test_small_vector_without_visual_is_silent(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("roads-2024")
    assert findings_for(validate(catalog.write()), "PTL-VIZ-004") == []


def test_clean_default_collection_has_no_viz_findings(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("roads-2024")
    report = validate(catalog.write())
    for rule in ("PTL-VIZ-001", "PTL-VIZ-002", "PTL-VIZ-003", "PTL-VIZ-004"):
        assert findings_for(report, rule) == []


def test_style_with_wrong_type_in_pmtiles_collection(catalog: CatalogBuilder) -> None:
    style = _style_asset()
    style["type"] = "application/json"
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "tiles": _pmtiles_asset(),
            "default-style": style,
        },
    )
    collection.item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: (
            d["links"].append(dict(_PMTILES_LINK)),
            d["stac_extensions"].append(_WEB_MAP_LINKS_URI),
        ),
    )
    findings = findings_for(validate(root), "PTL-VIZ-005")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/assets/default-style/type"
    assert "application/vnd.mapbox.style+json" in findings[0].message


def test_style_with_correct_type_in_pmtiles_collection_passes(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "tiles": _pmtiles_asset(),
            "default-style": _style_asset(),
        },
    )
    collection.item("roads-2024")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: (
            d["links"].append(dict(_PMTILES_LINK)),
            d["stac_extensions"].append(_WEB_MAP_LINKS_URI),
        ),
    )
    assert findings_for(validate(root), "PTL-VIZ-005") == []


def test_style_type_unchecked_without_pmtiles(catalog: CatalogBuilder) -> None:
    style = _style_asset()
    style["type"] = "application/json"
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "default-style": style,
        },
    )
    collection.item("roads-2024")
    assert findings_for(validate(catalog.write()), "PTL-VIZ-005") == []


def _named_style(href: str, *, default: bool = False) -> dict:
    asset = _style_asset()
    asset["href"] = href
    if default:
        asset["roles"] = ["style", "default"]
    return asset


def test_multiple_styles_without_default_role(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "style-categorical": _named_style("./styles/categorical.json"),
            "style-labeled": _named_style("./styles/labeled.json"),
        },
    )
    collection.item("roads-2024")
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-006")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "'default' role" in findings[0].message


def test_multiple_styles_with_default_role_passes(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "style-categorical": _named_style("./styles/categorical.json", default=True),
            "style-labeled": _named_style("./styles/labeled.json"),
        },
    )
    collection.item("roads-2024")
    assert findings_for(validate(catalog.write()), "PTL-VIZ-006") == []


def test_two_styles_marked_default_is_an_error(catalog: CatalogBuilder) -> None:
    """The spec says exactly one, so a second marker is as unusable as none."""
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "style-categorical": _named_style("./styles/categorical.json", default=True),
            "style-labeled": _named_style("./styles/labeled.json", default=True),
        },
    )
    collection.item("roads-2024")
    findings = findings_for(validate(catalog.write()), "PTL-VIZ-006")
    assert len(findings) == 1
    assert "style-categorical, style-labeled" in findings[0].message


def test_single_style_needs_no_default_role(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "roads",
        assets={
            "data": default_asset(),
            "thumbnail": thumbnail_asset(),
            "style-categorical": _named_style("./styles/categorical.json"),
        },
    )
    collection.item("roads-2024")
    assert findings_for(validate(catalog.write()), "PTL-VIZ-006") == []
