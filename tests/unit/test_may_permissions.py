"""Permission tests: rashid accepts what a ``MAY`` requirement permits.

A ``MUST`` or ``SHOULD`` in the spec's requirements manifest earns a check that
cites it, and ``tests/unit/test_spec_coverage.py`` gates that citation. A
``MAY`` grants the publisher a permission instead, so there is usually nothing
for a check to cite. The obligation it puts on a validator is the negative one:
do not report the permitted form.

Each test here builds one permitted form and asserts rashid reports nothing.
The whole report is asserted rather than one rule id, because the risk a
permission test guards against is an unrelated rule widening until it swallows
the permission. The forms themselves live in ``tests/property/permitted.py``,
which ``tests/property/test_invariance.py`` composes over generated catalogs.

:data:`PERMISSION_TESTS` maps each requirement to the test that covers it.
``test_spec_coverage.py`` reads the mapping and resolves every name against
this module, so deleting a test fails the coverage gate rather than quietly
dropping a requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rashid_core import ValidationOptions, validate
from tests.conftest import CatalogBuilder, default_asset, mirror_providers, mutate_json
from tests.property import permitted

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
    "PORTO-FMT-035": "test_converted_source_may_be_retained_alongside_the_parquet",
    "PORTO-ZARR-005": "test_a_zarr_store_may_declare_an_s3_href_beside_the_https_one",
}


def _collection_json(root: Path, collection_id: str) -> Path:
    return root / collection_id / "collection.json"


def _roads(catalog: CatalogBuilder) -> Path:
    """One collection, ``roads``, written; returns the root."""
    catalog.collection("roads", title="Road Centerlines")
    return catalog.write()


# --- core.md ---------------------------------------------------------------


def test_catalogs_may_nest_into_sub_catalogs(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-003: catalogs that only organize MAY nest into sub-catalogs."""
    catalog.collection("roads", title="Road Centerlines")
    europe = catalog.subcatalog("europe", title="Europe")
    netherlands = europe.subcatalog("netherlands", title="Netherlands")
    netherlands.collection("buildings", title="Building Footprints")
    report = validate(catalog.write())
    assert report.findings == []


# Partitioned trees name a bucket glob, which the data pass would list.
_METADATA = ValidationOptions(data=False)


def test_partitioned_collection_may_represent_partitions_as_items(
    catalog: CatalogBuilder,
) -> None:
    """PORTO-CORE-021: partitions MAY be items, and equally MAY not be."""
    catalog.collection("roads", title="Road Centerlines").item("roads-noord-holland")
    root = catalog.write()
    mutate_json(_collection_json(root, "roads"), permitted.partition_fields)
    # The glob names a bucket the data pass would list; the permission is metadata.
    with_items = validate(root, options=_METADATA)
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
    without_items = validate(root, options=_METADATA)
    assert without_items.findings == []


def test_non_cloud_native_alternate_may_accompany_the_primary(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-031: a GeoJSON MAY sit alongside the required GeoParquet."""
    root = _roads(catalog)
    mutate_json(_collection_json(root, "roads"), permitted.geojson_alternate)
    assert validate(root).findings == []


def test_one_organization_may_hold_multiple_roles(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-049: one provider MAY be both producer and host."""
    root = _roads(catalog)
    mutate_json(_collection_json(root, "roads"), permitted.one_provider_many_roles)
    assert validate(root).findings == []


def test_catalogs_may_declare_providers(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-050: a catalog MAY declare providers of its own."""
    root = _roads(catalog)
    permitted.catalog_providers(root)
    assert validate(root).findings == []


def test_collection_may_add_the_contacts_extension(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-052: contacts MAY be added on top of the provider url."""
    root = _roads(catalog)
    mutate_json(_collection_json(root, "roads"), permitted.contacts)
    assert validate(root).findings == []


def test_via_and_canonical_may_point_at_different_targets(catalog: CatalogBuilder) -> None:
    """PORTO-CORE-056: via MAY be a human page while canonical is a STAC root."""
    collection = catalog.collection("census", title="Census Tracts")
    collection.overrides["providers"] = mirror_providers()
    collection.overrides["updated"] = "2026-07-01T12:00:00Z"
    catalog.overrides["updated"] = "2026-07-01T12:00:00Z"
    root = catalog.write()
    mutate_json(_collection_json(root, "census"), permitted.mirror_links)
    assert validate(root).findings == []


# --- formats.md ------------------------------------------------------------


def test_pmtiles_may_be_registered_as_an_asset_and_a_link(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-013: the pmtiles link and a collection-level asset MAY coexist."""
    catalog.collection("roads", title="Road Centerlines").item("roads-2024")
    root = catalog.write()
    mutate_json(_collection_json(root, "roads"), permitted.pmtiles_asset_and_link)
    assert validate(root).findings == []


def test_large_files_may_be_partitioned(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-016: a large file MAY be partitioned."""
    root = _roads(catalog)
    mutate_json(_collection_json(root, "roads"), permitted.partition_fields)
    assert validate(root, options=_METADATA).findings == []


def test_converted_source_may_be_retained_alongside_the_parquet(catalog: CatalogBuilder) -> None:
    """PORTO-FMT-035: a converted CSV MAY be retained with the Parquet primary."""
    catalog.collection(
        "prices",
        title="Property Transaction Prices",
        assets={"data": default_asset()},
        **{"table:columns": [{"name": "year", "type": "int64", "description": "Sale year."}]},
    )
    root = catalog.write()
    mutate_json(_collection_json(root, "prices"), permitted.converted_source)
    assert validate(root).findings == []


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
    root = _roads(catalog)
    permitted.language_tree(root)
    assert validate(root).findings == []


# --- incubating/zarr.md ----------------------------------------------------


def test_a_zarr_store_may_declare_an_s3_href_beside_the_https_one(
    catalog: CatalogBuilder,
) -> None:
    """PORTO-ZARR-005: an ``s3://`` href MAY sit beside the ``https`` one.

    The STAC spelling for a second location of one object is the Alternate
    Assets extension, so the permitted form is an ``alternate`` member on the
    Zarr asset rather than a second asset. ``PTL-AST-002`` bans a non-https
    asset href, and the two coexist only because that rule reads each asset's
    own ``href`` and never the alternates beneath it. The whole report is
    asserted, because the Zarr checks read the same asset and could reach the
    alternate as easily.
    """
    catalog.collection("temps", title="Daily Temperatures")
    root = catalog.write()
    permitted.zarr_s3_alternate(root)
    assert validate(root).findings == []
