"""Tests for PTL-CLS-001..006, the classification class lists on raster bands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit

_CLASSIFICATION_URI = "https://stac-extensions.github.io/classification/v2.0.0/schema.json"


def _flood_classes() -> list[dict[str, Any]]:
    """The geohazards-tep flood classes, as portolan-spec#41 proposes them."""
    return [
        {"value": 0, "name": "no-data", "title": "No data", "nodata": True},
        {"value": 1, "name": "unchanged", "title": "Unchanged", "color_hint": "000000"},
        {"value": 2, "name": "permanent-water", "title": "Permanent water", "color_hint": "0000FF"},
        {"value": 3, "name": "flood", "title": "Flood", "color_hint": "00FF00"},
        {"value": 4, "name": "receded", "title": "Receded water", "color_hint": "00FFFF"},
    ]


def _raster_asset() -> dict[str, Any]:
    return {
        "href": "./flood.tif",
        "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        "roles": ["data"],
        "bands": [
            {"nodata": 0, "data_type": "uint8", "classification:classes": _flood_classes()},
        ],
    }


def _collection(root: Path) -> Path:
    return root / "flood" / "collection.json"


def _built(catalog: CatalogBuilder, mutate: Any = None) -> Path:
    catalog.collection("flood")
    root = catalog.write()

    def classify(data: dict[str, Any]) -> None:
        data["stac_extensions"] = [*data["stac_extensions"], _CLASSIFICATION_URI]
        data["assets"]["data"] = _raster_asset()
        if mutate is not None:
            mutate(data)

    mutate_json(_collection(root), classify)
    return root


def _classes(data: dict[str, Any]) -> list[dict[str, Any]]:
    classes: list[dict[str, Any]] = data["assets"]["data"]["bands"][0]["classification:classes"]
    return classes


_ALL = tuple(f"PTL-CLS-00{n}" for n in range(1, 7))
_POINTER = "/assets/data/bands/0/classification:classes"


def test_well_formed_band_classes_are_clean(catalog: CatalogBuilder) -> None:
    report = validate(_built(catalog))
    assert [f for rule in _ALL for f in findings_for(report, rule)] == []


def test_collection_without_classes_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("flood")
    report = validate(catalog.write())
    assert [f for rule in _ALL for f in findings_for(report, rule)] == []


# --- PTL-CLS-001 -------------------------------------------------------------


def test_undeclared_extension_is_flagged(catalog: CatalogBuilder) -> None:
    def drop(data: dict[str, Any]) -> None:
        data["stac_extensions"].remove(_CLASSIFICATION_URI)

    findings = findings_for(validate(_built(catalog, drop)), "PTL-CLS-001")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/stac_extensions"
    assert _CLASSIFICATION_URI in findings[0].fix_hint


def test_older_extension_version_counts_as_declared(catalog: CatalogBuilder) -> None:
    def older(data: dict[str, Any]) -> None:
        data["stac_extensions"].remove(_CLASSIFICATION_URI)
        data["stac_extensions"].append(_CLASSIFICATION_URI.replace("v2.0.0", "v1.1.0"))

    assert findings_for(validate(_built(catalog, older)), "PTL-CLS-001") == []


def test_item_property_bands_need_the_extension(catalog: CatalogBuilder) -> None:
    catalog.collection("flood").item(
        "scene",
        properties={
            "datetime": "2024-01-01T00:00:00Z",
            "bands": [{"nodata": 0, "classification:classes": _flood_classes()}],
        },
    )
    findings = findings_for(validate(catalog.write()), "PTL-CLS-001")
    assert [f.object_id for f in findings] == ["scene"]


# --- PTL-CLS-002 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        (lambda c: c[1].pop("value"), "/1/value"),
        (lambda c: c[1].update(value=1.5), "/1/value"),
        (lambda c: c[1].update(value=True), "/1/value"),
        (lambda c: c[2].update(value=1), "/2/value"),
        (lambda c: c[1].pop("name"), "/1/name"),
        (lambda c: c[1].update(name="permanent water"), "/1/name"),
        (lambda c: c[2].update(name="unchanged"), "/2/name"),
        (lambda c: c[1].pop("title"), "/1/title"),
        (lambda c: c[1].update(title=" "), "/1/title"),
        (lambda c: c[1].update(color_hint="#000000"), "/1/color_hint"),
        (lambda c: c[1].update(color_hint="00F"), "/1/color_hint"),
        (lambda c: c[1].update(nodata="yes"), "/1/nodata"),
        (lambda c: c.__setitem__(1, "unchanged"), "/1"),
    ],
    ids=[
        "no-value",
        "float-value",
        "bool-value",
        "duplicate-value",
        "no-name",
        "name-with-space",
        "duplicate-name",
        "no-title",
        "blank-title",
        "hash-color",
        "short-color",
        "non-boolean-nodata",
        "not-an-object",
    ],
)
def test_malformed_class_is_flagged(catalog: CatalogBuilder, change: Any, pointer: str) -> None:
    findings = findings_for(validate(_built(catalog, lambda d: change(_classes(d)))), "PTL-CLS-002")
    assert [f.json_pointer for f in findings] == [f"{_POINTER}{pointer}"]
    assert findings[0].fix_hint


def test_hash_prefixed_color_hint_suggests_the_bare_digits(catalog: CatalogBuilder) -> None:
    root = _built(catalog, lambda d: _classes(d)[2].update(color_hint="#0000ff"))
    (finding,) = findings_for(validate(root), "PTL-CLS-002")
    assert "'0000FF'" in finding.fix_hint


@pytest.mark.parametrize("classes", [[], {"value": 1}])
def test_empty_or_non_list_classes_are_flagged(catalog: CatalogBuilder, classes: Any) -> None:
    def replace(data: dict[str, Any]) -> None:
        data["assets"]["data"]["bands"][0]["classification:classes"] = classes

    findings = findings_for(validate(_built(catalog, replace)), "PTL-CLS-002")
    assert [f.json_pointer for f in findings] == [_POINTER]


def test_legacy_raster_bands_are_checked(catalog: CatalogBuilder) -> None:
    def legacy(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        asset["raster:bands"] = asset.pop("bands")
        asset["raster:bands"][0]["classification:classes"][1]["name"] = "un changed"

    findings = findings_for(validate(_built(catalog, legacy)), "PTL-CLS-002")
    assert [f.json_pointer for f in findings] == [
        "/assets/data/raster:bands/0/classification:classes/1/name"
    ]


def test_item_assets_classes_are_checked(catalog: CatalogBuilder) -> None:
    def template(data: dict[str, Any]) -> None:
        data["item_assets"] = {"data": _raster_asset()}
        data["item_assets"]["data"]["bands"][0]["classification:classes"][3]["color_hint"] = "lime"

    findings = findings_for(validate(_built(catalog, template)), "PTL-CLS-002")
    assert [f.json_pointer for f in findings] == [
        "/item_assets/data/bands/0/classification:classes/3/color_hint"
    ]


# --- PTL-CLS-003 -------------------------------------------------------------


def test_nodata_class_with_another_value_is_flagged(catalog: CatalogBuilder) -> None:
    def move(data: dict[str, Any]) -> None:
        data["assets"]["data"]["bands"][0]["nodata"] = 255

    findings = findings_for(validate(_built(catalog, move)), "PTL-CLS-003")
    assert [f.json_pointer for f in findings] == [f"{_POINTER}/0/value"]
    assert (findings[0].expected, findings[0].actual) == (255, 0)


def test_nodata_class_without_band_nodata_is_flagged(catalog: CatalogBuilder) -> None:
    def drop(data: dict[str, Any]) -> None:
        del data["assets"]["data"]["bands"][0]["nodata"]

    findings = findings_for(validate(_built(catalog, drop)), "PTL-CLS-003")
    assert [f.json_pointer for f in findings] == [f"{_POINTER}/0/nodata"]


def test_band_inherits_nodata_from_its_asset(catalog: CatalogBuilder) -> None:
    def hoist(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        asset["nodata"] = asset["bands"][0].pop("nodata")

    assert findings_for(validate(_built(catalog, hoist)), "PTL-CLS-003") == []


@pytest.mark.parametrize("nodata", [0.0, "nan"])
def test_band_nodata_compares_as_a_number(catalog: CatalogBuilder, nodata: Any) -> None:
    def set_nodata(data: dict[str, Any]) -> None:
        data["assets"]["data"]["bands"][0]["nodata"] = nodata

    findings = findings_for(validate(_built(catalog, set_nodata)), "PTL-CLS-003")
    assert len(findings) == (0 if nodata == 0.0 else 1)


# --- PTL-CLS-004 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "value", "flagged"),
    [
        ("uint8", 255, False),
        ("uint8", 256, True),
        ("uint8", -1, True),
        ("int8", -128, False),
        ("int8", 128, True),
        ("uint16", 65535, False),
        ("int16", 40000, True),
        ("float32", 100000, False),
        ("other", 100000, False),
    ],
)
def test_class_value_must_fit_the_data_type(
    catalog: CatalogBuilder, data_type: str, value: int, flagged: bool
) -> None:
    def widen(data: dict[str, Any]) -> None:
        data["assets"]["data"]["bands"][0]["data_type"] = data_type
        _classes(data)[4]["value"] = value

    findings = findings_for(validate(_built(catalog, widen)), "PTL-CLS-004")
    assert [f.json_pointer for f in findings] == ([f"{_POINTER}/4/value"] if flagged else [])


def test_band_inherits_data_type_from_its_asset(catalog: CatalogBuilder) -> None:
    def hoist(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        del asset["bands"][0]["data_type"]
        asset["data_type"] = "uint8"
        _classes(data)[4]["value"] = 300

    findings = findings_for(validate(_built(catalog, hoist)), "PTL-CLS-004")
    assert len(findings) == 1


def test_asset_level_classes_are_checked(catalog: CatalogBuilder) -> None:
    def flatten(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        band = asset.pop("bands")[0]
        asset.update(band)
        asset["classification:classes"][4]["value"] = 999

    findings = findings_for(validate(_built(catalog, flatten)), "PTL-CLS-004")
    assert [f.json_pointer for f in findings] == ["/assets/data/classification:classes/4/value"]


# --- PTL-CLS-005 -------------------------------------------------------------


def test_asset_level_classes_belong_on_a_band(catalog: CatalogBuilder) -> None:
    def flatten(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        asset.update(asset.pop("bands")[0])

    findings = findings_for(validate(_built(catalog, flatten)), "PTL-CLS-005")
    assert [f.json_pointer for f in findings] == ["/assets/data/classification:classes"]
    assert findings[0].severity.value == "error"


def test_legacy_raster_bands_alone_are_flagged(catalog: CatalogBuilder) -> None:
    def legacy(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        asset["raster:bands"] = asset.pop("bands")

    findings = findings_for(validate(_built(catalog, legacy)), "PTL-CLS-005")
    assert [f.json_pointer for f in findings] == [
        "/assets/data/raster:bands/0/classification:classes"
    ]
    assert "bands/0" in findings[0].fix_hint


def test_legacy_raster_bands_mirroring_bands_pass(catalog: CatalogBuilder) -> None:
    def mirror(data: dict[str, Any]) -> None:
        asset = data["assets"]["data"]
        asset["raster:bands"] = [dict(band) for band in asset["bands"]]

    assert findings_for(validate(_built(catalog, mirror)), "PTL-CLS-005") == []


# --- PTL-CLS-006 -------------------------------------------------------------


def _scene_catalog(catalog: CatalogBuilder, template: dict[str, Any] | None) -> Path:
    built = catalog.collection("flood")
    built.item("scene-1")
    built.item("scene-2")
    root = catalog.write()

    def classify(data: dict[str, Any]) -> None:
        data["stac_extensions"] = [*data.get("stac_extensions", []), _CLASSIFICATION_URI]
        data["assets"] = {"data": _raster_asset()}

    for item_id in ("scene-1", "scene-2"):
        for path in (root / "flood").rglob(f"{item_id}.json"):
            mutate_json(path, classify)

    def collection(data: dict[str, Any]) -> None:
        data["stac_extensions"] = [*data["stac_extensions"], _CLASSIFICATION_URI]
        if template is not None:
            data["item_assets"] = {"data": template}

    mutate_json(_collection(root), collection)
    return root


def test_item_classes_missing_from_item_assets_warn_once(catalog: CatalogBuilder) -> None:
    findings = findings_for(validate(_scene_catalog(catalog, None)), "PTL-CLS-006")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/item_assets/data"
    assert findings[0].severity.value == "warning"
    assert "item_assets/data/bands/0/classification:classes" in findings[0].fix_hint


def test_item_assets_without_the_band_classes_warn(catalog: CatalogBuilder) -> None:
    template = _raster_asset()
    del template["bands"][0]["classification:classes"]
    findings = findings_for(validate(_scene_catalog(catalog, template)), "PTL-CLS-006")
    assert len(findings) == 1


def test_item_assets_carrying_the_classes_pass(catalog: CatalogBuilder) -> None:
    report = validate(_scene_catalog(catalog, _raster_asset()))
    assert [f for rule in _ALL for f in findings_for(report, rule)] == []
