"""Forms the spec permits, as transforms of a written catalog tree.

Each transform takes the root directory of a valid tree and rewrites it into a
form a ``MAY`` permits; the result must still validate clean. The permission
tests in ``tests/unit/test_may_permissions.py`` apply one form to a hand-built
tree, and ``tests/property/test_invariance.py`` composes them over generated
trees. Every transform applies to every object it can, so it does not depend
on the shape of the tree it is given.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.conftest import (
    LANGUAGE_URI,
    default_asset,
    default_providers,
    mutate_json,
)

Transform = Callable[[Path], None]

PARTITION_URI = "https://schemas.portolan-sdi.org/incubating/partition/v1.0.0/schema.json"
CONTACTS_URI = "https://stac-extensions.github.io/contacts/v1.0.0/schema.json"
WEB_MAP_LINKS_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"
ALTERNATE_ASSETS_URI = "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json"
ZARR_STORES = Path(__file__).resolve().parents[1] / "fixtures" / "zarr-stores"
#: The directory a translated tree is written to. An underscore cannot start a
#: generated id, so it never collides with a collection or sub-catalog.
LANGUAGE_DIR = "_ro"


def _typed(root: Path, name: str, stac_type: str) -> list[Path]:
    # An item may be named "collection" or "catalog", so the filename alone is not enough.
    return sorted(
        path
        for path in root.rglob(name)
        if json.loads(path.read_text(encoding="utf-8")).get("type") == stac_type
    )


def collection_files(root: Path) -> list[Path]:
    return _typed(root, "collection.json", "Collection")


def catalog_files(root: Path) -> list[Path]:
    return _typed(root, "catalog.json", "Catalog")


def _each_collection(mutate: Callable[[dict[str, Any]], None]) -> Transform:
    def transform(root: Path) -> None:
        for path in collection_files(root):
            mutate_json(path, mutate)

    transform.__name__ = mutate.__name__
    return transform


def _is_mirror(data: dict[str, Any]) -> bool:
    """core.md, Provenance: a mirror's producer and host are different providers."""
    return len(data.get("providers", [])) > 1


def _extend(data: dict[str, Any], uri: str) -> None:
    if uri not in data["stac_extensions"]:
        data["stac_extensions"] = [*data["stac_extensions"], uri]


# --- core.md ---------------------------------------------------------------


def mirror_links(data: dict[str, Any]) -> None:
    """PORTO-CORE-056: via and canonical MAY both be present and point at
    different targets, a human-readable page and a STAC root."""
    data["links"].extend(
        [
            {"rel": "via", "href": "https://source.example.org/data", "type": "text/html"},
            {
                "rel": "canonical",
                "href": "https://source.example.org/stac/catalog.json",
                "type": "application/json",
            },
        ]
    )


def partition_fields(data: dict[str, Any]) -> None:
    """PORTO-CORE-021 (partitions MAY be items, or not) and PORTO-FMT-016
    (large files MAY be partitioned): the partition extension and its fields."""
    data["partition:scheme"] = "directory"
    data["partition:keys"] = [{"name": "province", "type": "string"}]
    data["partition:glob"] = "s3://demo-bucket/roads/province=*/*.parquet"
    _extend(data, PARTITION_URI)


def geojson_alternate(data: dict[str, Any]) -> None:
    """PORTO-CORE-031: a GeoJSON MAY sit alongside the required GeoParquet."""
    geojson = default_asset()
    geojson.update(href="./data.geojson", type="application/geo+json")
    data["assets"]["data-geojson"] = geojson


def one_provider_many_roles(data: dict[str, Any]) -> None:
    """PORTO-CORE-049: one organization MAY hold multiple roles. A mirror is
    left alone: one provider holding both roles would make it official."""
    if not _is_mirror(data):
        data["providers"][-1]["roles"] = ["producer", "processor", "licensor", "host"]


def contacts(data: dict[str, Any]) -> None:
    """PORTO-CORE-052: a collection MAY add the contacts extension."""
    _extend(data, CONTACTS_URI)
    data["contacts"] = [
        {
            "name": "Demo Org Data Desk",
            "organization": "Demo Org",
            "emails": [{"value": "data@example.org", "roles": ["work"]}],
            "roles": ["pointOfContact"],
        }
    ]


def service_via(data: dict[str, Any]) -> None:
    """PORTO-CORE-084: additional via links MAY reference upstream services,
    typed with the media type each service returns. Only a mirror has via links."""
    if _is_mirror(data):
        data["links"].append(
            {
                "rel": "via",
                "href": "https://source.example.org/ogc/collections/data/items",
                "type": "application/geo+json",
            }
        )


def catalog_providers(root: Path) -> None:
    """PORTO-CORE-050: catalogs MAY also declare providers."""
    for path in catalog_files(root):
        mutate_json(path, lambda data: data.__setitem__("providers", default_providers()))


def language_tree(root: Path, tag: str = "ro") -> None:
    """PORTO-CORE-079: a catalog MAY publish one STAC object tree per language,
    the root linking each translated root with an ``alternate`` link, as the
    STAC Language extension defines. The translation repeats every ID."""
    target = root / LANGUAGE_DIR
    shutil.copytree(root, target, ignore=lambda d, _: [LANGUAGE_DIR] if Path(d) == root else [])

    def declare(data: dict[str, Any], code: str, other: str, href: str) -> None:
        _extend(data, LANGUAGE_URI)
        data["language"] = {"code": code, "name": code}
        data["languages"] = [{"code": other, "name": other}]
        data["links"].append(
            {
                "rel": "alternate",
                "href": href,
                "type": "application/json",
                "hreflang": other,
                "title": other,
            }
        )

    mutate_json(
        root / "catalog.json",
        lambda data: declare(data, "en", tag, f"./{LANGUAGE_DIR}/catalog.json"),
    )
    mutate_json(target / "catalog.json", lambda data: declare(data, tag, "en", "../catalog.json"))


# --- formats.md ------------------------------------------------------------


def pmtiles_asset_and_link(data: dict[str, Any]) -> None:
    """PORTO-FMT-013: a PMTiles file MAY be both a rel:'pmtiles' link and a
    collection-level asset. Its style is PORTO-FMT-014's requirement."""
    tiles = default_asset()
    tiles.update(href="./data.pmtiles", type="application/vnd.pmtiles", roles=["visual"])
    style = default_asset()
    style.update(
        href="./styles/default.json", type="application/vnd.mapbox.style+json", roles=["style"]
    )
    data["assets"].update({"tiles": tiles, "default-style": style})
    data["links"].append(
        {
            "rel": "pmtiles",
            "href": "./data.pmtiles",
            "type": "application/vnd.pmtiles",
            "pmtiles:layers": ["data"],
        }
    )
    _extend(data, WEB_MAP_LINKS_URI)


def converted_source(data: dict[str, Any]) -> None:
    """PORTO-FMT-035: a converted CSV MAY be retained beside the Parquet primary."""
    csv = default_asset()
    csv.update(href="./source.csv", type="text/csv", roles=["source"])
    data["assets"]["source"] = csv


# --- incubating/zarr.md ----------------------------------------------------


_ZARR_TYPE = "application/vnd.zarr; version=3"


def _zarr_data(path: Path, store: str | None) -> None:
    """Make a collection's data a Zarr store, copying ``store`` from the
    fixtures; ``None`` keeps a store already there and copies ``flat`` otherwise."""
    target = path.parent / "data.zarr"
    if store is not None and target.exists():
        shutil.rmtree(target)
    if not target.exists():
        shutil.copytree(ZARR_STORES / (store or "flat"), target)

    def to_zarr(data: dict[str, Any]) -> None:
        if data["assets"]["data"].get("type") == _ZARR_TYPE:
            return
        zarr = default_asset()
        zarr.update(href="./data.zarr", type=_ZARR_TYPE)
        zarr["bands"] = [
            {"name": "temp", "nodata": -9999, "statistics": {"minimum": -50.0, "maximum": 50.0}}
        ]
        data["assets"]["data"] = zarr

    mutate_json(path, to_zarr)


def zarr_s3_alternate(root: Path) -> None:
    """PORTO-ZARR-005: an ``s3://`` href MAY sit beside the ``https`` one.

    The STAC spelling for a second location of one object is the Alternate
    Assets extension, so each collection's data becomes a Zarr store whose
    asset carries an ``s3`` alternate.
    """
    for path in collection_files(root):
        _zarr_data(path, None)

        def add_alternate(data: dict[str, Any]) -> None:
            data["assets"]["data"]["alternate"] = {
                "s3": {"href": "s3://example-bucket/temps/data.zarr", "title": "S3 access"}
            }
            _extend(data, ALTERNATE_ASSETS_URI)

        mutate_json(path, add_alternate)


def zarr_big_shards(root: Path) -> None:
    """PORTO-ZARR-024: a shard MAY be larger on X and Y than the inner chunk
    bounds allow."""
    for path in collection_files(root):
        _zarr_data(path, "big-shard")


def table_columns_per_asset(data: dict[str, Any]) -> None:
    """PORTO-FMT-048: data assets MAY declare ``table:columns`` per asset. The
    data is GeoParquet, so the columns name its geometry."""
    data["assets"]["data"]["table:columns"] = [
        {"name": "geometry", "type": "binary", "description": "Feature geometry."},
        {"name": "year", "type": "int64", "description": "Survey year."},
    ]


#: Each permitted form keyed by the requirement that permits it. The forms in
#: :data:`METADATA_ONLY` name a bucket the data pass would list, so a tree
#: carrying one is validated without the data pass.
FORMS: dict[str, Transform] = {
    "PORTO-CORE-021": _each_collection(partition_fields),
    "PORTO-CORE-031": _each_collection(geojson_alternate),
    "PORTO-CORE-049": _each_collection(one_provider_many_roles),
    "PORTO-CORE-050": catalog_providers,
    "PORTO-CORE-052": _each_collection(contacts),
    "PORTO-CORE-079": language_tree,
    "PORTO-CORE-084": _each_collection(service_via),
    "PORTO-FMT-013": _each_collection(pmtiles_asset_and_link),
    "PORTO-FMT-035": _each_collection(converted_source),
    "PORTO-FMT-048": _each_collection(table_columns_per_asset),
    "PORTO-ZARR-005": zarr_s3_alternate,
    "PORTO-ZARR-024": zarr_big_shards,
}
METADATA_ONLY = frozenset({"PORTO-CORE-021"})
#: Forms that describe the data as GeoParquet, and forms that replace it with
#: a Zarr store. A composition draws from one family: PMTiles must reference
#: GeoParquet (PORTO-FMT-062), an alternate must have an equivalent primary
#: (PORTO-CORE-031), and Parquet is the primary of a converted CSV (PORTO-FMT-035).
GEOPARQUET_FORMS = frozenset(
    {"PORTO-CORE-021", "PORTO-CORE-031", "PORTO-FMT-013", "PORTO-FMT-035", "PORTO-FMT-048"}
)
ZARR_FORMS = frozenset({"PORTO-ZARR-005", "PORTO-ZARR-024"})
