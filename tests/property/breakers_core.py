"""Breakers for every check outside the data and live passes.

Each breaker edits a valid generated tree (``strategies.catalogs(min_items=1)``)
so that its rule is violated, and returns the options to validate with. Other
rules may fire too; the property only asks that this one does. The recipes
follow the unit tests named in each docstring.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rashid_core import ValidationOptions
from rashid_core._jsonschema import SchemaError
from rashid_core._profile import bundled_profile_versions, profile_uri
from tests.conftest import (
    PORTOLAN_URI,
    CollectionBuilder,
    default_asset,
    default_providers,
    mirror_providers,
    mutate_json,
    thumbnail_asset,
)
from tests.property.permitted import LANGUAGE_DIR, language_tree, pmtiles_asset_and_link
from tests.property.registry import Target, breaks
from tests.unit.test_remote import DirFetcher

Data = dict[str, Any]

_COG = "image/tiff; application=geotiff; profile=cloud-optimized"
_PMTILES = "application/vnd.pmtiles"
_STYLE = "application/vnd.mapbox.style+json"
_CLASSIFICATION_URI = "https://stac-extensions.github.io/classification/v2.0.0/schema.json"
_PROJECTION_URI = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
_PARTITION_URI = "https://schemas.portolan-sdi.org/incubating/partition/v1.0.0/schema.json"
#: A name no generated id can take: generated ids have no uppercase letter.
_EXTRA = "Zz"


# --- locating and adding objects --------------------------------------------


def _collection_dir(root: Path) -> Path:
    """The first root collection that holds an item; the strategy guarantees one."""
    for path in sorted(root.glob("*/collection.json")):
        if any(link["rel"] == "item" for link in json.loads(path.read_text("utf-8"))["links"]):
            return path.parent
    raise AssertionError("the base strategy promised a root collection with an item")


def _collection(root: Path) -> Path:
    return _collection_dir(root) / "collection.json"


def _items(root: Path) -> list[Path]:
    return sorted(p for p in _collection_dir(root).glob("*/*.json") if p.stem == p.parent.name)


def _edit(path: Path, mutate: Callable[[Data], object]) -> None:
    mutate_json(path, lambda data: (mutate(data), None)[1])


def _edit_collection(mutate: Callable[[Data], object]) -> Callable[[Path], None]:
    """A breaker that applies ``mutate`` to the collection :func:`_collection` names."""

    def breaker(root: Path) -> None:
        _edit(_collection(root), mutate)

    return breaker


def _asset(**fields: Any) -> Data:
    asset = default_asset()
    asset.update(fields)
    return asset


def _drop_links(data: Data, rel: str) -> None:
    data["links"] = [link for link in data["links"] if link["rel"] != rel]


def _add_collection(
    root: Path, *, items: int = 0, item_fields: Data | None = None, **overrides: Any
) -> Path:
    """Write one more root collection in directory ``Zz`` and link it; returns its file."""
    built = CollectionBuilder("extra-layer", 0, "Extra Layer", **overrides)
    for index in range(items):
        built.item(f"extra-{index}", **(item_fields or {}))
    directory = root / _EXTRA
    built.write(directory)
    mutate_json(
        root / "catalog.json",
        lambda data: data["links"].append(
            {
                "rel": "child",
                "href": f"./{_EXTRA}/collection.json",
                "type": "application/json",
                "title": "Extra Layer",
            }
        ),
    )
    return directory / "collection.json"


# --- runner -----------------------------------------------------------------


@breaks("PTL-GEN-000")
def _no_root(root: Path) -> None:
    (root / "catalog.json").unlink()


@breaks("PTL-GEN-001")
def _unparseable(root: Path) -> None:
    _collection(root).write_text("{not json", encoding="utf-8")


@breaks("PTL-GEN-002")
def _read_by_url(root: Path) -> Target:
    """test_remote.py: a tree read by following links from a URL is partial."""
    fetcher = DirFetcher(root)
    return Target(
        f"{fetcher.base}catalog.json", ValidationOptions(fetcher=fetcher, live=False, data=False)
    )


# --- structural, extensions, schema -----------------------------------------


def _failing_validator(*_: object) -> list[SchemaError]:
    raise RuntimeError("validator unavailable")


@breaks("PTL-STR-000")
def _structural_unavailable(_root: Path) -> ValidationOptions:
    """test_structural.py: a validator that raises is one warning."""
    return ValidationOptions(structural_validator=_failing_validator)


@breaks("PTL-STR-001")
def _not_stac(root: Path) -> None:
    _edit(_collection(root), lambda data: data.pop("extent"))


@breaks("PTL-EXT-000")
def _extension_schema_unreachable(root: Path) -> ValidationOptions:
    """An unregistered schema the pass may fetch, but only over https, so the
    fetch fails before it reaches the network."""
    uri = "http://example.org/unreachable/v1.0.0/schema.json"
    _edit(_collection(root), lambda data: data["stac_extensions"].append(uri))
    return ValidationOptions(schema_allow_network=True)


@breaks("PTL-EXT-001")
def _extension_violated(root: Path) -> None:
    """test_extensions.py: Projection v2 requires proj:code to be a string."""

    def mutate(data: Data) -> None:
        data["stac_extensions"].append(_PROJECTION_URI)
        data["assets"]["data"]["proj:code"] = 4326

    _edit(_collection(root), mutate)


@breaks("PTL-EXT-002")
def _extension_unregistered(root: Path) -> None:
    uri = "https://stac-extensions.github.io/eo/v1.1.0/schema.json"
    _edit(_collection(root), lambda data: data["stac_extensions"].append(uri))


@breaks("PTL-SCH-000")
def _schema_unavailable(_root: Path) -> ValidationOptions:
    """test_schema.py: a validator that raises is one warning."""
    return ValidationOptions(schema=True, schema_validator=_failing_validator)


@breaks("PTL-SCH-001")
def _profile_schema_violated(root: Path) -> ValidationOptions:
    _edit(_collection(root), lambda data: data.pop("providers"))
    return ValidationOptions(schema=True)


@breaks("PTL-SCH-002")
def _schema_map_unused(_root: Path) -> ValidationOptions:
    """test_schema.py: a mapping for a URI no object declares is never applied."""
    unused = "https://schemas.portolan-sdi.org/portolan/v99.0.0/schema.json"
    return ValidationOptions(schema=True, schema_map={unused: {}})


# --- files ------------------------------------------------------------------


@breaks("PTL-FIL-001")
def _no_agents_file(root: Path) -> None:
    (_collection_dir(root) / "AGENTS.md").unlink()


@breaks("PTL-FIL-002")
def _no_agents_link(root: Path) -> None:
    _edit(_collection(root), lambda data: _drop_links(data, "agents"))


@breaks("PTL-FIL-003")
def _no_readme_link(root: Path) -> None:
    _edit(_collection(root), lambda data: _drop_links(data, "describedby"))


@breaks("PTL-FIL-004")
def _empty_readme(root: Path) -> None:
    (_collection_dir(root) / "README.md").write_text("", encoding="utf-8")


@breaks("PTL-FIL-005")
def _readme_without_sections(root: Path) -> None:
    (_collection_dir(root) / "README.md").write_text("# Demo\n\nSome words.\n", encoding="utf-8")


# --- titles -----------------------------------------------------------------


breaks("PTL-TTL-001")(_edit_collection(lambda data: data.__setitem__("title", "")))
breaks("PTL-TTL-002")(_edit_collection(lambda data: data.__setitem__("title", "road_lines")))


@breaks("PTL-TTL-003")
def _untitled_child_link(root: Path) -> None:
    def mutate(data: Data) -> None:
        for link in data["links"]:
            if link["rel"] == "child":
                link.pop("title")

    _edit(root / "catalog.json", mutate)


# --- links ------------------------------------------------------------------


breaks("PTL-LNK-001")(_edit_collection(lambda data: _drop_links(data, "parent")))


@breaks("PTL-LNK-002")
def _unreachable(root: Path) -> None:
    _edit(root / "catalog.json", lambda data: _drop_links(data, "child"))


@breaks("PTL-LNK-003")
def _child_link_type(root: Path) -> None:
    def mutate(data: Data) -> None:
        for link in data["links"]:
            if link["rel"] == "child":
                link["type"] = "text/plain"

    _edit(root / "catalog.json", mutate)


@breaks("PTL-LNK-006")
def _unresolved_parent(root: Path) -> None:
    def mutate(data: Data) -> None:
        for link in data["links"]:
            if link["rel"] == "parent":
                link["href"] = "../missing/catalog.json"

    _edit(_collection(root), mutate)


def _icon(**fields: Any) -> Callable[[Path], None]:
    link = {"rel": "icon", "href": "./icon.png", "type": "image/png", "title": "Logo", **fields}
    return _edit_collection(lambda data: data["links"].append(dict(link)))


breaks("PTL-LNK-007")(_icon(type="image/tiff"))
breaks("PTL-LNK-008")(_icon(title=""))
breaks("PTL-LNK-009")(_icon(href="https://example.org/icon.png"))


@breaks("PTL-LNK-010")
def _child_into_translation(root: Path) -> None:
    """test_rules_links.py: linking the translation as a child."""
    language_tree(root)
    _edit(
        root / "catalog.json",
        lambda data: data["links"].append(
            {
                "rel": "child",
                "href": f"./{LANGUAGE_DIR}/catalog.json",
                "type": "application/json",
                "title": "Romanian",
            }
        ),
    )


# --- extents ----------------------------------------------------------------


breaks("PTL-BBX-001")(
    _edit_collection(
        lambda data: data["extent"]["spatial"].__setitem__("bbox", [[4.0, 52.0, 6.0, 50.0]])
    )
)


@breaks("PTL-TMP-001")
def _no_datetime(root: Path) -> None:
    _edit(_items(root)[0], lambda data: data["properties"].__setitem__("datetime", None))


@breaks("PTL-TMP-002")
def _bad_datetime(root: Path) -> None:
    _edit(_items(root)[0], lambda data: data["properties"].__setitem__("datetime", "yesterday"))


# --- providers, license, provenance -----------------------------------------


def _providers(*providers: Data) -> Callable[[Path], None]:
    return _edit_collection(lambda data: data.__setitem__("providers", list(providers)))


breaks("PTL-PRV-001")(_providers({"name": "A", "roles": ["host"], "url": "https://a.example"}))
breaks("PTL-PRV-002")(
    _providers(
        {"name": "A", "roles": ["producer", "host"], "url": "https://a.example"},
        {"name": "B", "roles": ["host"], "url": "https://b.example"},
    )
)
breaks("PTL-PRV-003")(_providers({"name": "A", "roles": ["producer", "host"]}))

breaks("PTL-LIC-001")(_edit_collection(lambda data: data.__setitem__("license", "")))
breaks("PTL-LIC-002")(_edit_collection(lambda data: data.__setitem__("license", "other")))
breaks("PTL-LIC-003")(_edit_collection(lambda data: data.__setitem__("license", "proprietary")))


def _mirror(via: bool, canonical: bool, updated: bool) -> Callable[[Path], None]:
    """A mirror collection, with each of its provenance fields on or off."""

    def mutate(data: Data) -> None:
        data["providers"] = mirror_providers()
        _drop_links(data, "via")
        _drop_links(data, "canonical")
        data.pop("updated", None)
        if via:
            data["links"].append(
                {"rel": "via", "href": "https://source.example.org", "type": "text/html"}
            )
        if canonical:
            data["links"].append(
                {"rel": "canonical", "href": "https://source.example.org/stac/catalog.json"}
            )
        if updated:
            data["updated"] = "2026-07-01T12:00:00Z"

    return _edit_collection(mutate)


breaks("PTL-PRO-001")(_mirror(via=False, canonical=True, updated=True))
breaks("PTL-PRO-002")(_mirror(via=True, canonical=False, updated=True))
breaks("PTL-PRO-003")(_mirror(via=True, canonical=True, updated=False))


@breaks("PTL-PRO-004")
def _official_with_upstream(root: Path) -> None:
    def mutate(data: Data) -> None:
        data["providers"] = default_providers()
        data["links"].append({"rel": "via", "href": "https://example.org", "type": "text/html"})

    _edit(_collection(root), mutate)


# --- assets -----------------------------------------------------------------


def _data_asset(**fields: Any) -> Callable[[Path], None]:
    return _edit_collection(lambda data: data["assets"]["data"].update(fields))


breaks("PTL-AST-002")(_data_asset(href="s3://bucket/data.parquet"))
breaks("PTL-AST-012")(_data_asset(href="./data-*.parquet"))
breaks("PTL-AST-010")(_data_asset(title=""))
breaks("PTL-AST-006")(_data_asset(href="./data.tif", type="image/tiff; application=geotiff"))
breaks("PTL-AST-004")(
    _data_asset(
        **{"file:checksum": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b8"}
    )
)
breaks("PTL-AST-001")(_edit_collection(lambda data: data["assets"]["data"].pop("roles")))
breaks("PTL-AST-003")(_edit_collection(lambda data: data["assets"]["data"].pop("file:size")))


@breaks("PTL-AST-007")
def _collection_without_data(root: Path) -> None:
    """An itemless collection with no data asset; items would carry it instead."""
    _add_collection(root, assets={"thumbnail": thumbnail_asset()})


@breaks("PTL-AST-005")
def _catalog_asset(root: Path) -> None:
    _edit(root / "catalog.json", lambda data: data.__setitem__("assets", {"x": default_asset()}))


@breaks("PTL-AST-008")
def _item_without_data(root: Path) -> None:
    _edit(_items(root)[0], lambda data: data.__setitem__("assets", {"t": thumbnail_asset()}))


breaks("PTL-AST-009")(
    _edit_collection(
        lambda data: data["assets"].__setitem__(
            "map", _asset(href="./map.pmtiles", type=_PMTILES, roles=["data"])
        )
    )
)


# --- conformance ------------------------------------------------------------


breaks("PTL-CNF-001")(_edit_collection(lambda data: data.pop("stac_extensions")))
breaks("PTL-CNF-002")(
    _edit_collection(
        lambda data: data.__setitem__(
            "stac_extensions",
            [
                profile_uri(
                    next(v for v in bundled_profile_versions() if profile_uri(v) != PORTOLAN_URI)
                )
            ],
        )
    )
)
breaks("PTL-CNF-003")(_edit_collection(lambda data: data.__setitem__("version", "2")))


@breaks("PTL-CNF-004")
def _stale_extension(root: Path) -> ValidationOptions:
    """test_rules_conformance.py: Raster v1.1.0 is behind the registry's v2.0.0.
    The extension pass would fetch the stale schema, so it is off."""
    stale = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"
    _edit(_collection(root), lambda data: data["stac_extensions"].append(stale))
    return ValidationOptions(extensions=False)


@breaks("PTL-CNF-005")
def _newer_profile(root: Path) -> None:
    _edit(
        root / "catalog.json",
        lambda data: data.__setitem__("stac_extensions", [profile_uri("v99.0.0")]),
    )


@breaks("PTL-VER-001")
def _dangling_version_link(root: Path) -> None:
    """test_rules_versions.py: a version link must resolve."""
    _edit(
        _items(root)[0],
        lambda data: data["links"].append({"rel": "successor-version", "href": "./gone.json"}),
    )


# --- visualization ----------------------------------------------------------


breaks("PTL-VIZ-001")(_edit_collection(lambda data: data["assets"].pop("thumbnail")))
breaks("PTL-VIZ-002")(
    _edit_collection(lambda data: data["assets"].__setitem__("preview", _asset(roles=["visual"])))
)
breaks("PTL-VIZ-003")(
    _edit_collection(
        lambda data: data["assets"].__setitem__(
            "tiles", _asset(href="./tiles.pmtiles", type=_PMTILES, roles=["visual"])
        )
    )
)


@breaks("PTL-VIZ-005")
def _style_typed_json(root: Path) -> None:
    def mutate(data: Data) -> None:
        pmtiles_asset_and_link(data)
        data["assets"]["default-style"]["type"] = "application/json"

    _edit(_collection(root), mutate)


@breaks("PTL-VIZ-006")
def _two_unmarked_styles(root: Path) -> None:
    def mutate(data: Data) -> None:
        for name in ("a", "b"):
            data["assets"][f"style-{name}"] = _asset(
                href=f"./styles/{name}.json", type=_STYLE, roles=["style"]
            )

    _edit(_collection(root), mutate)


breaks("PTL-VIZ-004")(_data_asset(**{"file:size": 553_395_618}))


@breaks("PTL-VIZ-007")
def _mosaic_without_overview(root: Path) -> None:
    """test_rules_viz.py: three COG items tiling the extent, and no overview."""
    path = _add_collection(root, items=3, assets={"thumbnail": thumbnail_asset()})
    for index, item in enumerate(sorted(path.parent.glob("*/*.json"))):
        west, south = 4.0 + index % 2, 50.0 + index // 2 % 2

        def mutate(data: Data, west: float = west, south: float = south) -> None:
            data["bbox"] = [west, south, west + 1.0, south + 1.0]
            data["assets"]["data"].update(href=f"./{data['id']}.tif", type=_COG)

        _edit(item, mutate)


breaks("PTL-VIZ-008")(
    _edit_collection(
        lambda data: data["assets"].__setitem__(
            "overview", _asset(href="./overview.tif", type=_COG, roles=["visual", "overview"])
        )
    )
)


# --- partitions -------------------------------------------------------------


@breaks("PTL-PRT-001")
def _partition_scheme_alone(root: Path) -> ValidationOptions:
    _edit(_collection(root), lambda data: data.__setitem__("partition:scheme", "hive"))
    return ValidationOptions(data=False)


@breaks("PTL-PRT-002")
def _relative_partition_glob(root: Path) -> ValidationOptions:
    def mutate(data: Data) -> None:
        data["stac_extensions"].append(_PARTITION_URI)
        data["partition:scheme"] = "directory"
        data["partition:keys"] = [{"name": "province", "type": "string"}]
        data["partition:glob"] = "./province=*/*.parquet"

    _edit(_collection(root), mutate)
    return ValidationOptions(data=False)


# --- classification ---------------------------------------------------------


def _classified(**overrides: Any) -> Data:
    """test_rules_classification.py: a two-band COG, classes on the first band."""
    band = {
        "nodata": 0,
        "data_type": "uint8",
        "classification:classes": [
            {"value": 0, "name": "no-data", "title": "No data", "nodata": True},
            {"value": 1, "name": "flood", "title": "Flood"},
        ],
    }
    band.update(overrides)
    return _asset(href="./flood.tif", type=_COG, bands=[band, {"name": "confidence"}])


def _classify(asset: Data, extension: bool = True) -> Callable[[Path], None]:
    def mutate(data: Data) -> None:
        if extension:
            data["stac_extensions"].append(_CLASSIFICATION_URI)
        data["assets"]["data"] = asset

    return _edit_collection(mutate)


breaks("PTL-CLS-001")(_classify(_classified(), extension=False))
breaks("PTL-CLS-002")(
    _classify(_classified(**{"classification:classes": [{"value": 1, "name": "flood"}]}))
)
breaks("PTL-CLS-003")(_classify(_classified(nodata=255)))


@breaks("PTL-CLS-005")
def _classes_on_a_multi_band_asset(root: Path) -> None:
    asset = _classified()
    asset["classification:classes"] = asset["bands"][0].pop("classification:classes")
    _classify(asset)(root)


@breaks("PTL-CLS-006")
def _item_classes_without_item_assets(root: Path) -> None:
    """test_rules_classification.py: classified items, no item_assets template."""

    def mutate(data: Data) -> None:
        data["stac_extensions"] = [*data.get("stac_extensions", []), _CLASSIFICATION_URI]
        data["assets"] = {"data": _classified()}

    for item in _items(root):
        _edit(item, mutate)
    _edit(_collection(root), lambda data: data["stac_extensions"].append(_CLASSIFICATION_URI))


# --- collections ------------------------------------------------------------


@breaks("PTL-COL-001")
def _lone_item_holds_the_data(root: Path) -> None:
    """test_rules_collections.py: one item wraps the collection's only data file."""
    _add_collection(root, items=1, assets={"thumbnail": thumbnail_asset()})


@breaks("PTL-COL-004")
def _scenes_at_collection_level(root: Path) -> None:
    def mutate(data: Data) -> None:
        data["assets"]["data"].update(href="./scene-a.tif", type=_COG)
        data["assets"]["scene-b"] = _asset(href="./scene-b.tif", type=_COG)

    _edit(_collection(root), mutate)


@breaks("PTL-COL-005")
def _scene_files_without_items(root: Path) -> None:
    """test_rules_collections.py: scene files on disk, no items for them."""
    directory = _add_collection(root).parent
    for name in ("pop-1975.tif", "pop-1990.tif"):
        (directory / name).write_bytes(b"II*\x00")


@breaks("PTL-COL-002")
def _nested_collection(root: Path) -> None:
    """test_finding_quality.py: a collection file below another collection."""
    outer = _collection_dir(root)
    nested = outer / _EXTRA
    nested.mkdir()
    for name in ("collection.json", "AGENTS.md", "README.md"):
        shutil.copy(outer / name, nested / name)
    _edit(nested / "collection.json", lambda data: data.__setitem__("id", "nested"))


breaks("PTL-COL-003")(_edit_collection(lambda data: data.__setitem__("id", "Bad Id")))


@breaks("PTL-CAT-001")
def _twenty_items(root: Path) -> None:
    _add_collection(root, items=20)


@breaks("PTL-MIR-001")
def _scenes_without_a_mirror(root: Path) -> None:
    """test_rules_item_mirror.py: two COG scene items and no items.parquet."""
    path = _add_collection(root, items=2)
    _edit(path, lambda data: data["assets"].pop("data"))
    for item in sorted(path.parent.glob("*/*.json")):
        _edit(
            item, lambda data: data["assets"]["data"].update(href=f"./{data['id']}.tif", type=_COG)
        )


breaks("PTL-MIR-002")(
    _edit_collection(
        lambda data: data["assets"].__setitem__(
            "items", _asset(href="./items.parquet", type="application/json", roles=["metadata"])
        )
    )
)
