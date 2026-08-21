"""Permission tests: rashid accepts what a ``MAY`` requirement permits.

A ``MUST`` or ``SHOULD`` in the spec's requirements manifest earns a check that
cites it, and ``tests/unit/test_spec_coverage.py`` gates that citation. A
``MAY`` grants the publisher a permission instead, so there is usually nothing
for a check to cite. The obligation it puts on a validator is the negative one:
do not report the permitted form.

Each test here builds one permitted form and asserts rashid reports nothing.
The whole report is asserted rather than one rule id, because the risk a
permission test guards against is an unrelated rule widening until it swallows
the permission. ``PORTO-FMT-020`` is the worked case: it permits ``s3://`` and
``gs://`` in ``partition:glob`` while ``PORTO-CORE-023`` bans non-https absolute
asset hrefs. The two coexist only because ``PTL-AST-002`` reads
``node.data["assets"]`` and never the collection's own fields.

:data:`PERMISSION_TESTS` maps each requirement to the test that covers it.
``test_spec_coverage.py`` reads the mapping and resolves every name against
this module, so deleting a test fails the coverage gate rather than quietly
dropping a requirement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rashid import validate
from tests.conftest import (
    CatalogBuilder,
    default_asset,
    default_providers,
    findings_for,
    mutate_json,
    thumbnail_asset,
    write_language_trees,
)

pytestmark = pytest.mark.unit

#: Every ``MAY`` + ``enforcement: validator`` requirement that no check cites,
#: mapped to the test below that proves rashid accepts what it permits.
#: ``PORTO-CORE-055`` and ``PORTO-CORE-066`` are absent because checks cite
#: them: ``PTL-PRV-004`` and ``PTL-VIZ-004`` both emit at INFO, which is what
#: those two requirements permit a validator to surface.
PERMISSION_TESTS: dict[str, str] = {
    "PORTO-CORE-003": "test_catalogs_may_nest_into_sub_catalogs",
    "PORTO-CORE-021": "test_partitioned_collection_may_represent_partitions_as_items",
    "PORTO-CORE-031": "test_non_cloud_native_alternate_may_accompany_the_primary",
    "PORTO-CORE-049": "test_one_organization_may_hold_multiple_roles",
    "PORTO-CORE-050": "test_catalogs_may_declare_providers",
    "PORTO-CORE-052": "test_collection_may_add_the_contacts_extension",
    "PORTO-CORE-056": "test_via_and_canonical_may_point_at_different_targets",
    "PORTO-CORE-079": "test_a_catalog_may_publish_an_alternate_language_tree",
    "PORTO-FMT-013": "test_pmtiles_may_be_registered_as_an_asset_and_a_link",
    "PORTO-FMT-016": "test_large_files_may_be_partitioned",
    "PORTO-FMT-020": "test_partition_glob_may_use_bucket_native_schemes",
    "PORTO-FMT-035": "test_converted_source_may_be_retained_alongside_the_parquet",
}

_PARTITION_URI = "https://schemas.portolan-sdi.org/incubating/partition/v1.0.0/schema.json"
_CONTACTS_URI = "https://stac-extensions.github.io/contacts/v0.1.1/schema.json"
_WEB_MAP_LINKS_URI = "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json"
_PMTILES_LINK = {
    "rel": "pmtiles",
    "href": "./data.pmtiles",
    "type": "application/vnd.pmtiles",
    "pmtiles:layers": ["data"],
}


def _partition_fields(data: dict[str, Any], glob: str = "./province=*/*.parquet") -> None:
    """Declare the partition extension and its three required fields."""
    data["partition:scheme"] = "directory"
    data["partition:keys"] = [{"name": "province", "type": "string"}]
    data["partition:glob"] = glob
    data["stac_extensions"] = [*data["stac_extensions"], _PARTITION_URI]


def _pmtiles_asset() -> dict[str, Any]:
    asset = default_asset()
    asset["href"] = "./data.pmtiles"
    asset["type"] = "application/vnd.pmtiles"
    asset["roles"] = ["visual"]
    return asset


def _style_asset() -> dict[str, Any]:
    asset = default_asset()
    asset["href"] = "./styles/default.json"
    asset["type"] = "application/vnd.mapbox.style+json"
    asset["roles"] = ["style"]
    return asset


def _collection_json(root: Path, collection_id: str) -> Path:
    return root / collection_id / "collection.json"


# --- core.md ---------------------------------------------------------------


def test_catalogs_may_nest_into_sub_catalogs(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-003: catalogs that only organize MAY nest into sub-catalogs."""
    catalog.collection("roads", title="Road Centerlines")
    europe = catalog.subcatalog("europe", title="Europe")
    netherlands = europe.subcatalog("netherlands", title="Netherlands")
    netherlands.collection("buildings", title="Building Footprints")
    report = validate(catalog.write())
    assert report.findings == []


def test_partitioned_collection_may_represent_partitions_as_items(
    catalog: CatalogBuilder,
) -> None:
    """PORTO-CORE-021: partitions MAY be items, and equally MAY not be."""
    catalog.collection("roads", title="Road Centerlines").item("roads-noord-holland")
    root = catalog.write()
    mutate_json(_collection_json(root, "roads"), _partition_fields)
    with_items = validate(root)
    assert with_items.findings == []

    # The same collection without the item: the permission runs both ways, so
    # neither shape may be reported.
    mutate_json(
        _collection_json(root, "roads"),
        lambda data: data.__setitem__(
            "links", [link for link in data["links"] if link.get("rel") != "item"]
        ),
    )
    (root / "roads" / "roads-noord-holland" / "roads-noord-holland.json").unlink()
    (root / "roads" / "roads-noord-holland").rmdir()
    without_items = validate(root)
    assert without_items.findings == []


def test_non_cloud_native_alternate_may_accompany_the_primary(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-031: a GeoJSON MAY sit alongside the required GeoParquet."""
    geojson = default_asset()
    geojson["href"] = "./roads.geojson"
    geojson["type"] = "application/geo+json"
    geojson["roles"] = ["data"]
    catalog.collection(
        "roads",
        title="Road Centerlines",
        assets={
            "data": default_asset(),
            "roads-geojson": geojson,
            "thumbnail": thumbnail_asset(),
        },
    )
    report = validate(catalog.write())
    assert report.findings == []


def test_one_organization_may_hold_multiple_roles(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-049: one provider MAY be both producer and host."""
    providers = [
        {
            "name": "Demo Org",
            "roles": ["producer", "processor", "host", "licensor"],
            "url": "https://example.org/contact",
        }
    ]
    catalog.collection("roads", title="Road Centerlines", providers=providers)
    report = validate(catalog.write())
    assert report.findings == []


def test_catalogs_may_declare_providers(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-050: a catalog MAY declare providers of its own."""
    catalog.overrides["providers"] = default_providers()
    catalog.collection("roads", title="Road Centerlines")
    report = validate(catalog.write())
    assert report.findings == []


def test_collection_may_add_the_contacts_extension(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-052: contacts MAY be added on top of the provider url."""
    catalog.collection("roads", title="Road Centerlines")
    root = catalog.write()

    def add_contacts(data: dict[str, Any]) -> None:
        data["stac_extensions"] = [*data["stac_extensions"], _CONTACTS_URI]
        data["contacts"] = [
            {
                "name": "Demo Org Data Desk",
                "organization": "Demo Org",
                "emails": [{"value": "data@example.org", "roles": ["work"]}],
                "roles": ["pointOfContact"],
            }
        ]

    mutate_json(_collection_json(root, "roads"), add_contacts)
    report = validate(root)
    assert report.findings == []


def test_via_and_canonical_may_point_at_different_targets(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-056: via MAY be a human page while canonical is a STAC root."""
    collection = catalog.collection("census", title="Census Tracts")
    collection.overrides["providers"] = [
        {"name": "Source Org", "roles": ["producer"], "url": "https://source.example.org"},
        {"name": "Mirror Org", "roles": ["host"], "url": "https://mirror.example.org"},
    ]
    collection.overrides["updated"] = "2026-07-01T12:00:00Z"
    catalog.overrides["updated"] = "2026-07-01T12:00:00Z"
    root = catalog.write()
    mutate_json(
        _collection_json(root, "census"),
        lambda data: data["links"].extend(
            [
                {"rel": "via", "href": "https://source.example.org/census", "type": "text/html"},
                {
                    "rel": "canonical",
                    "href": "https://source.example.org/stac/catalog.json",
                    "type": "application/json",
                },
            ]
        ),
    )
    report = validate(root)
    assert report.findings == []


# --- formats.md ------------------------------------------------------------


def test_pmtiles_may_be_registered_as_an_asset_and_a_link(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-013: the pmtiles link and a collection-level asset MAY coexist."""
    collection = catalog.collection(
        "roads",
        title="Road Centerlines",
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
        _collection_json(root, "roads"),
        lambda data: (
            data["links"].append(dict(_PMTILES_LINK)),
            data["stac_extensions"].append(_WEB_MAP_LINKS_URI),
        ),
    )
    report = validate(root)
    assert report.findings == []


def test_large_files_may_be_partitioned(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-016: a large file MAY be partitioned."""
    catalog.collection("roads", title="Road Centerlines")
    root = catalog.write()
    mutate_json(_collection_json(root, "roads"), _partition_fields)
    report = validate(root)
    assert report.findings == []


@pytest.mark.parametrize(
    "glob",
    [
        "s3://demo-bucket/roads/province=*/*.parquet",
        "gs://demo-bucket/roads/province=*/*.parquet",
    ],
)
def test_partition_glob_may_use_bucket_native_schemes(catalog: CatalogBuilder, glob: str) -> None:
    """PORTO-FMT-020: the https-only asset rule does not reach partition:glob.

    PTL-AST-002 enforces PORTO-CORE-023 over ``node.data["assets"]`` alone. The
    same s3 URL is a finding in an asset href and no finding in the glob, and
    both halves are asserted here so widening the rule to the collection's own
    fields cannot pass silently.
    """
    catalog.collection("roads", title="Road Centerlines")
    root = catalog.write()
    mutate_json(_collection_json(root, "roads"), lambda data: _partition_fields(data, glob))
    report = validate(root)
    assert findings_for(report, "PTL-AST-002") == []
    assert report.findings == []

    # The other half of the separation: the same URL as an asset href is a finding.
    mutate_json(
        _collection_json(root, "roads"),
        lambda data: data["assets"]["data"].__setitem__("href", glob.replace("*", "all")),
    )
    assert len(findings_for(validate(root), "PTL-AST-002")) == 1


def test_converted_source_may_be_retained_alongside_the_parquet(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-035: a converted CSV MAY be retained with the Parquet primary."""
    csv = default_asset()
    csv["href"] = "./prices.csv"
    csv["type"] = "text/csv"
    csv["roles"] = ["source"]
    catalog.collection(
        "prices",
        title="Property Transaction Prices",
        assets={"data": default_asset(), "source": csv},
        **{"table:columns": [{"name": "year", "type": "int64"}]},
    )
    report = validate(catalog.write())
    assert report.findings == []


def test_a_catalog_may_publish_an_alternate_language_tree(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-079: a catalog MAY carry its metadata in more than one language.

    The tree the STAC Language extension prescribes looks like a stray
    sub-catalog to a walk of the file system. Before the carve-out landed,
    rashid asked it for a ``parent`` link, asked the root for a ``child`` link
    to it, and called the repeated collection ID a duplicate, so a publisher
    following the extension could not pass. The whole report is asserted,
    because any one of those three would defeat the permission on its own.

    The report stays empty now that the profile's registry pins the Language
    extension. The extension pass validates both roots against its schema and
    finds nothing, rather than reporting that it could not check them.
    """
    report = validate(write_language_trees(catalog))
    assert report.findings == []
