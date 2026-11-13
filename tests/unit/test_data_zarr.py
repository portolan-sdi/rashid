"""The incubating Zarr profile checks, over hand-written Zarr v3 metadata.

Each fixture under ``tests/fixtures/zarr-stores`` is a store's metadata and
nothing else: a tree of ``zarr.json`` documents, with no chunk data, because
every validator-enforced requirement of the profile is decidable from metadata.
``pyramid`` and ``flat`` are the conforming pair — one with consolidated
metadata and a sharded two-level pyramid, one without either — and every other
store breaks exactly what its name says.

The checks run through the real data pass, so what these assert is what
``rashid check`` reports for a catalog on disk.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from rashid import ValidationOptions, validate
from rashid._href import store_member_href
from rashid.data.reader import LocalOnlyReader
from rashid.model import Report, Severity
from rashid.registry import (
    CHECKS,
    ZAR_ASSET,
    ZAR_AXIS_ORDER,
    ZAR_CHUNK_SIZE,
    ZAR_CODECS,
    ZAR_CONVENTIONS,
    ZAR_CRS,
    ZAR_DISPLAY_RANGE,
    ZAR_LAYOUT_FIELDS,
    ZAR_LEVEL_STEP,
    ZAR_NODATA,
    ZAR_NONSPATIAL_CHUNK,
    ZAR_PYRAMID,
    ZAR_READ_AMPLIFICATION,
    ZAR_ROW_MAJOR,
    ZAR_SHARD_SPAN,
    ZAR_STORE_VERSION,
    ZAR_TRANSFORM,
    ZAR_VARIABLES,
    is_incubating,
)
from tests.conftest import CatalogBuilder, default_asset, thumbnail_asset

pytestmark = pytest.mark.unit

STORES = Path(__file__).resolve().parents[1] / "fixtures" / "zarr-stores"

ZARR_MEDIA_TYPE = "application/vnd.zarr; version=3"


#: A conforming asset for the ``flat`` and ``pyramid`` stores: one band named
#: after the store's one variable, with the statistics and no-data value the
#: display-range and no-data rules ask for.
def zarr_asset(**overrides: Any) -> dict[str, Any]:
    asset: dict[str, Any] = {
        "href": "./data.zarr",
        "type": ZARR_MEDIA_TYPE,
        "roles": ["data"],
        "bands": [
            {
                "name": "temp",
                "description": "Air temperature",
                "data_type": "float32",
                "nodata": -9999,
                "statistics": {"minimum": -50.0, "maximum": 50.0},
            }
        ],
    }
    asset.update(overrides)
    return asset


def write_store(
    catalog: CatalogBuilder, store: str, *, asset: dict[str, Any] | None = None
) -> Path:
    """Write a catalog whose one collection publishes ``store`` as its data asset."""
    catalog.collection(
        "temps",
        title="Daily Temperatures",
        assets={"data": asset or zarr_asset(), "thumbnail": thumbnail_asset()},
    )
    root = catalog.write()
    shutil.copytree(STORES / store, root / "temps" / "data.zarr")
    return root


def zarr_report(root: Path) -> Report:
    """Validate ``root`` with only the passes the Zarr checks need."""
    return validate(
        root,
        options=ValidationOptions(rules=(), structural=False, extensions=False),
    )


def zarr_findings(root: Path) -> dict[str, str]:
    """Every Zarr finding, as check id -> message, for one catalog."""
    report = zarr_report(root)
    return {f.rule_id: f.message for f in report.findings if is_incubating(f.rule_id)}


# --- the conforming pair ---------------------------------------------------


@pytest.mark.parametrize("store", ["pyramid", "flat"])
def test_a_conforming_store_reports_nothing(catalog: CatalogBuilder, store: str) -> None:
    assert zarr_findings(write_store(catalog, store)) == {}


def test_a_conforming_catalog_passes_every_pass(catalog: CatalogBuilder) -> None:
    """The whole validator accepts the store, not just the Zarr checks.

    ``file:size`` and ``file:checksum`` are absent because a store is a key
    prefix rather than one object, so ``PTL-AST-003`` warns; nothing else does.
    """
    report = validate(write_store(catalog, "pyramid"))
    assert {f.rule_id for f in report.findings} == {"PTL-AST-003"}


def test_a_catalog_without_a_zarr_asset_reports_no_zarr_finding(
    catalog: CatalogBuilder,
) -> None:
    catalog.collection("roads").item("seg1")
    assert zarr_findings(catalog.write()) == {}


# --- store and asset -------------------------------------------------------


def test_a_zarr_v2_store_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "zarr-v2"))[ZAR_STORE_VERSION]
    assert "zarr_format 2" in message


def test_a_store_with_no_metadata_at_all_cannot_be_judged(catalog: CatalogBuilder) -> None:
    """A store rashid cannot open yields one INFO, and no grid or chunk verdict."""
    root = write_store(catalog, "flat")
    (root / "temps" / "data.zarr" / "zarr.json").unlink()
    report = zarr_report(root)
    findings = [f for f in report.findings if is_incubating(f.rule_id)]
    assert [(f.rule_id, f.severity) for f in findings] == [(ZAR_STORE_VERSION, Severity.INFO)]


def test_a_media_type_without_the_version_parameter_is_reported(
    catalog: CatalogBuilder,
) -> None:
    asset = zarr_asset(type="application/vnd.zarr")
    message = zarr_findings(write_store(catalog, "flat", asset=asset))[ZAR_ASSET]
    assert "version None" in message


@pytest.mark.parametrize(
    "media_type",
    [
        "application/vnd.zarr; version=3",
        "application/vnd.zarr;version=3",
        "application/vnd.zarr; version=3; profile=multiscales",
        "application/vnd.zarr; profile=multiscales; version=3",
        'application/vnd.zarr; version="3"',
    ],
)
def test_the_version_parameter_is_read_whatever_its_spelling(
    catalog: CatalogBuilder, media_type: str
) -> None:
    asset = zarr_asset(type=media_type)
    assert ZAR_ASSET not in zarr_findings(write_store(catalog, "flat", asset=asset))


def test_an_href_naming_an_array_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "array-root"))[ZAR_ASSET]
    assert "must name a group" in message


# --- conventions -----------------------------------------------------------


def test_undeclared_conventions_are_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "no-conventions"))[ZAR_CONVENTIONS]
    assert "spatial, proj" in message


def test_a_pyramid_without_the_multiscales_convention_is_reported(
    catalog: CatalogBuilder,
) -> None:
    """A store that needs a pyramid must declare the convention that defines one."""
    message = zarr_findings(write_store(catalog, "no-pyramid"))[ZAR_CONVENTIONS]
    assert "multiscales" in message


def test_a_malformed_convention_field_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "malformed-conventions"))[ZAR_CONVENTIONS]
    assert "spatial:transform is not six numbers" in message


# --- grid ------------------------------------------------------------------


def test_a_missing_spatial_transform_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "no-transform"))[ZAR_TRANSFORM]
    assert "no affine spatial:transform" in message


def test_spatial_axes_that_are_not_last_are_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "axis-order"))[ZAR_AXIS_ORDER]
    assert "['y', 'x', 'band']" in message


def test_a_transpose_codec_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "transposed"))[ZAR_ROW_MAJOR]
    assert "not stored row-major" in message


# --- coordinate reference system -------------------------------------------


def test_a_store_with_no_crs_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "no-crs"))[ZAR_CRS]
    assert "neither proj:code nor proj:projjson" in message


def test_projjson_without_a_code_is_information(catalog: CatalogBuilder) -> None:
    report = zarr_report(write_store(catalog, "projjson-only"))
    crs = [f for f in report.findings if f.rule_id == ZAR_CRS]
    assert [f.severity for f in crs] == [Severity.INFO]


def test_proj_on_the_parent_group_satisfies_the_store(catalog: CatalogBuilder) -> None:
    assert ZAR_CRS not in zarr_findings(write_store(catalog, "group-proj"))


def test_proj_two_groups_above_the_array_does_not_reach_it(catalog: CatalogBuilder) -> None:
    """The proj convention inherits to direct child arrays only."""
    assert ZAR_CRS in zarr_findings(write_store(catalog, "grandparent-proj"))


# --- overviews -------------------------------------------------------------


def test_a_wide_store_without_a_pyramid_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "no-pyramid"))[ZAR_PYRAMID]
    assert "16 chunks" in message and "declares no multiscale pyramid" in message


def test_a_pyramid_whose_coarsest_level_is_too_wide_is_reported(
    catalog: CatalogBuilder,
) -> None:
    message = zarr_findings(write_store(catalog, "coarse-pyramid"))[ZAR_PYRAMID]
    assert "coarsest level needs 16 chunks" in message


def test_layout_entries_without_the_spatial_fields_are_reported(
    catalog: CatalogBuilder,
) -> None:
    message = zarr_findings(write_store(catalog, "layout-fields"))[ZAR_LAYOUT_FIELDS]
    assert "0, 1" in message


def test_levels_that_step_by_three_are_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "step-three"))[ZAR_LEVEL_STEP]
    assert "0->1" in message


# --- variables -------------------------------------------------------------


def test_two_variables_without_cube_variables_are_reported(catalog: CatalogBuilder) -> None:
    asset = zarr_asset(bands=[{"name": "temp"}, {"name": "prec"}])
    message = zarr_findings(write_store(catalog, "two-variables", asset=asset))[ZAR_VARIABLES]
    assert "prec, temp" in message


def test_two_variables_with_cube_variables_are_accepted(catalog: CatalogBuilder) -> None:
    asset = zarr_asset(
        bands=[{"name": "temp"}, {"name": "prec"}],
        **{
            "cube:variables": {
                "temp": {"dimensions": ["y", "x"], "type": "data"},
                "prec": {"dimensions": ["y", "x"], "type": "data"},
            }
        },
    )
    assert ZAR_VARIABLES not in zarr_findings(write_store(catalog, "two-variables", asset=asset))


# --- display range and no data ---------------------------------------------


def test_a_band_without_statistics_or_a_rescale_is_reported(catalog: CatalogBuilder) -> None:
    asset = zarr_asset(bands=[{"name": "temp", "nodata": -9999}])
    message = zarr_findings(write_store(catalog, "flat", asset=asset))[ZAR_DISPLAY_RANGE]
    assert "no display range" in message


def test_a_renders_rescale_supplies_the_display_range(catalog: CatalogBuilder) -> None:
    """Any renders entry counts: the render extension cannot name one variable."""
    catalog.collection(
        "temps",
        title="Daily Temperatures",
        assets={
            "data": zarr_asset(bands=[{"name": "temp", "nodata": -9999}]),
            "thumbnail": thumbnail_asset(),
        },
        renders={"default": {"assets": ["data"], "rescale": [[-50, 50]]}},
    )
    root = catalog.write()
    shutil.copytree(STORES / "flat", root / "temps" / "data.zarr")
    assert ZAR_DISPLAY_RANGE not in zarr_findings(root)


def test_an_eight_bit_store_with_a_band_axis_of_four_is_exempt(catalog: CatalogBuilder) -> None:
    asset = zarr_asset(bands=[{"name": "rgba", "data_type": "uint8", "nodata": 0}])
    assert ZAR_DISPLAY_RANGE not in zarr_findings(write_store(catalog, "rgba", asset=asset))


def test_a_band_without_nodata_is_reported(catalog: CatalogBuilder) -> None:
    asset = zarr_asset(bands=[{"name": "temp", "statistics": {"minimum": 0, "maximum": 1}}])
    message = zarr_findings(write_store(catalog, "flat", asset=asset))[ZAR_NODATA]
    assert "no nodata" in message


def test_an_alpha_band_stands_in_for_nodata(catalog: CatalogBuilder) -> None:
    asset = zarr_asset(
        bands=[
            {"name": "temp", "statistics": {"minimum": 0, "maximum": 1}},
            {"name": "alpha", "data_type": "uint8"},
        ]
    )
    assert ZAR_NODATA not in zarr_findings(write_store(catalog, "flat", asset=asset))


# --- chunking --------------------------------------------------------------


def test_a_store_that_reads_sixteen_samples_per_sample_is_reported(
    catalog: CatalogBuilder,
) -> None:
    findings = zarr_findings(write_store(catalog, "time-chunked"))
    assert "16 samples" in findings[ZAR_READ_AMPLIFICATION]
    assert "['time']" in findings[ZAR_NONSPATIAL_CHUNK]


def test_a_non_spatial_chunk_within_the_ceiling_is_only_a_chunking_remark(
    catalog: CatalogBuilder,
) -> None:
    findings = zarr_findings(write_store(catalog, "time-chunked-four"))
    assert ZAR_READ_AMPLIFICATION not in findings
    assert ZAR_NONSPATIAL_CHUNK in findings


def test_chunks_below_the_floor_are_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "small-chunks"))[ZAR_CHUNK_SIZE]
    assert "'y': 64" in message


def test_a_shard_larger_than_a_chunk_is_permitted(catalog: CatalogBuilder) -> None:
    """PORTO-ZARR-024: only the inner chunk is judged against the sample range."""
    assert ZAR_CHUNK_SIZE not in zarr_findings(write_store(catalog, "big-shard"))


def test_a_shard_spanning_sixteen_timesteps_is_reported(catalog: CatalogBuilder) -> None:
    findings = zarr_findings(write_store(catalog, "wide-shard"))
    assert "16 samples" in findings[ZAR_SHARD_SPAN]
    assert ZAR_READ_AMPLIFICATION not in findings  # the inner chunk spans one timestep


def test_a_codec_without_a_javascript_implementation_is_reported(
    catalog: CatalogBuilder,
) -> None:
    message = zarr_findings(write_store(catalog, "unsupported-codec"))[ZAR_CODECS]
    assert "zfpy" in message


# --- the incubating cap -----------------------------------------------------


def test_no_zarr_check_declares_an_error() -> None:
    incubating = [info for info in CHECKS.values() if is_incubating(info.id)]
    assert incubating
    assert all(info.severity is not Severity.ERROR for info in incubating)


def test_a_broken_store_never_fails_the_catalog(catalog: CatalogBuilder) -> None:
    """Every requirement of the profile is incubating, so none can fail a run."""
    report = validate(write_store(catalog, "zarr-v2"))
    assert report.passed
    assert ZAR_STORE_VERSION in {f.rule_id for f in report.warnings}


def test_a_remote_store_is_left_unread_in_local_scope(catalog: CatalogBuilder) -> None:
    """``--data-scope local`` reads the catalog's own files and nothing else.

    The store reads go through the pass's asset reader, so narrowing the
    reader narrows them too: no request leaves, and the profile's verdict is
    that it could not be reached.
    """
    asset = zarr_asset(href="https://data.example.org/temps.zarr")
    catalog.collection(
        "temps",
        title="Daily Temperatures",
        assets={"data": asset, "thumbnail": thumbnail_asset()},
    )
    report = validate(
        catalog.write(),
        options=ValidationOptions(
            rules=(),
            structural=False,
            extensions=False,
            data_reader_factory=LocalOnlyReader,
        ),
    )
    findings = [f for f in report.findings if is_incubating(f.rule_id)]
    assert [(f.rule_id, f.severity) for f in findings] == [(ZAR_STORE_VERSION, Severity.INFO)]
    assert "not reachable" in findings[0].message


# --- defects found in review ------------------------------------------------


def test_a_non_finite_layout_shape_does_not_abort_the_data_pass(
    catalog: CatalogBuilder,
) -> None:
    """A shape that overflows to infinity must not raise out of the pass.

    ``1e999`` is a valid JSON number literal that every parser reads as
    infinity, and ``int(inf)`` raises. The exception used to escape the check,
    which made ``validate_data`` report one ``PTL-DAT-000`` warning and abandon
    every object after it, so a catalog with real errors later in the tree
    passed.
    """
    report = zarr_report(write_store(catalog, "non-finite-shape"))
    assert [f.rule_id for f in report.findings if f.rule_id == "PTL-DAT-000"] == []


def test_a_broken_store_does_not_hide_a_later_object(catalog: CatalogBuilder) -> None:
    """An error after the broken store is still reported.

    This is what the abandoned pass cost: the collections are checked in path
    order, so a store that raised in ``a-temps`` silenced ``z-temps``.
    """
    bad = zarr_asset(**{"file:size": 1})
    catalog.collection(
        "a-temps",
        title="Daily Temperatures A",
        assets={"data": bad, "thumbnail": thumbnail_asset()},
    )
    catalog.collection(
        "z-temps",
        title="Daily Temperatures Z",
        assets={"data": default_asset(), "thumbnail": thumbnail_asset()},
    )
    root = catalog.write()
    shutil.copytree(STORES / "non-finite-shape", root / "a-temps" / "data.zarr")
    (root / "z-temps" / "data.parquet").write_bytes(b"PAR1not-really-parquet")
    report = zarr_report(root)
    assert "PTL-DAT-002" in {f.rule_id for f in report.findings}


def test_an_array_without_dimension_names_is_still_renderable(
    catalog: CatalogBuilder,
) -> None:
    """``dimension_names`` is optional in Zarr v3, so its absence hides nothing.

    The store below breaks the chunk-size and transform rules. Before the fix
    it reported neither, because no axis could be named.
    """
    findings = zarr_findings(write_store(catalog, "unnamed-dimensions"))
    assert ZAR_CHUNK_SIZE in findings
    assert ZAR_TRANSFORM in findings


def test_two_dimensional_coordinate_arrays_are_not_data_variables(
    catalog: CatalogBuilder,
) -> None:
    """A curvilinear store names its coordinates as 2-D arrays over the same axes.

    ``lat`` and ``lon`` carry both spatial dimensions, so an axis test alone
    reads them as renderable arrays and reports three data variables where the
    store publishes one.
    """
    asset = zarr_asset(
        bands=[{"name": "z", "nodata": -9999, "statistics": {"minimum": 0, "maximum": 1}}]
    )
    findings = zarr_findings(write_store(catalog, "curvilinear", asset=asset))
    assert ZAR_VARIABLES not in findings
    assert ZAR_DISPLAY_RANGE not in findings
    assert ZAR_NODATA not in findings


def test_a_cube_variable_that_is_not_data_is_not_a_data_variable(
    catalog: CatalogBuilder,
) -> None:
    """``cube:variables`` types a coordinate variable as such; respect it."""
    asset = zarr_asset(
        bands=[{"name": "z", "nodata": -9999, "statistics": {"minimum": 0, "maximum": 1}}],
        **{
            "cube:variables": {
                "z": {"dimensions": ["y", "x"], "type": "data"},
                "lat": {"dimensions": ["y", "x"], "type": "auxiliary"},
                "lon": {"dimensions": ["y", "x"], "type": "auxiliary"},
            }
        },
    )
    assert ZAR_NODATA not in zarr_findings(write_store(catalog, "curvilinear", asset=asset))


def test_repeated_spatial_dimensions_are_malformed(catalog: CatalogBuilder) -> None:
    """One name cannot be both the Y and the X axis."""
    message = zarr_findings(write_store(catalog, "duplicate-dimensions"))[ZAR_CONVENTIONS]
    assert "spatial:dimensions" in message


def test_an_identity_transpose_codec_is_accepted(catalog: CatalogBuilder) -> None:
    assert ZAR_ROW_MAJOR not in zarr_findings(write_store(catalog, "identity-transpose"))


def test_a_transpose_inside_the_sharding_codec_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "sharded-transpose"))[ZAR_ROW_MAJOR]
    assert "[1, 0]" in message


def test_a_transform_type_that_is_not_affine_is_reported(catalog: CatalogBuilder) -> None:
    message = zarr_findings(write_store(catalog, "transform-type"))[ZAR_TRANSFORM]
    assert "'rpc'" in message


def test_a_layout_entry_without_an_asset_is_reported(catalog: CatalogBuilder) -> None:
    """A level that names no group is a layout defect, not a level to drop."""
    message = zarr_findings(write_store(catalog, "layout-without-asset"))[ZAR_LAYOUT_FIELDS]
    assert "names no asset" in message


def test_the_eight_bit_exemption_applies_to_one_array_only(catalog: CatalogBuilder) -> None:
    """A uint8 RGBA array beside a float32 array exempts only itself."""
    asset = zarr_asset(
        bands=[{"name": "rgba", "data_type": "uint8", "nodata": 0}, {"name": "temp", "nodata": -1}],
        **{
            "cube:variables": {
                "rgba": {"dimensions": ["y", "x", "band"], "type": "data"},
                "temp": {"dimensions": ["y", "x"], "type": "data"},
            }
        },
    )
    message = zarr_findings(write_store(catalog, "mixed-sample-types", asset=asset))[
        ZAR_DISPLAY_RANGE
    ]
    assert "temp" in message
    assert "rgba" not in message


def test_axes_named_only_by_the_convention_measure_the_levels(
    catalog: CatalogBuilder,
) -> None:
    """A store whose axes have no well-known spelling is still measurable."""
    assert zarr_findings(write_store(catalog, "named-axes-only")) == {}


def test_levels_that_halve_by_a_ceiling_division_are_accepted(
    catalog: CatalogBuilder,
) -> None:
    """2050 -> 1025 -> 513 halves exactly, within the rounding a shape forces."""
    assert ZAR_LEVEL_STEP not in zarr_findings(write_store(catalog, "ceiling-steps"))


def test_a_store_larger_than_the_document_budget_says_so(catalog: CatalogBuilder) -> None:
    """A store rashid read only part of cannot be judged in full, and says so.

    The asset names more variables than the budget allows reads for, and the
    store carries no consolidated metadata, so each one costs a request.
    """
    bands = [{"name": f"v{index:03d}"} for index in range(80)]
    asset = zarr_asset(bands=bands)
    report = zarr_report(write_store(catalog, "flat", asset=asset))
    partial = [f for f in report.findings if f.rule_id == ZAR_STORE_VERSION]
    assert [f.severity for f in partial] == [Severity.INFO]
    assert "part of the store" in partial[0].message


def test_a_signed_store_url_keeps_its_query_string() -> None:
    """A member href must carry the signature the store href was given."""
    assert (
        store_member_href("https://host/x.zarr?sig=abc&exp=1", "zarr.json")
        == "https://host/x.zarr/zarr.json?sig=abc&exp=1"
    )
    assert store_member_href("https://host/x.zarr/?sig=abc", "0/temp") == (
        "https://host/x.zarr/0/temp?sig=abc"
    )
