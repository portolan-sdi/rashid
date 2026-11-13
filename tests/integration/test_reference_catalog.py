"""The spec's reference catalog, vendored, must pass rashid with zero errors.

This is the dogfood guard. ``portolan-spec``'s
``examples/catalog/portolan-reference`` is
the canonical example of a valid Portolan catalog; it is vendored here (JSON and
Markdown only) by ``scripts/vendor_spec_fixtures.py`` and validated by both the
metadata pass and the profile schema pass. The only findings allowed are the two
INFO mirror-canonical nudges. A regression here means rashid has diverged from the
spec it validates — exactly the schema-URI drift these guards exist to catch.

The schema pass uses a bundled release when available. An unreleased catalog
URI uses the test-only draft fixture from spec main through ``schema_map``.
Both paths are offline.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rashid import ValidationOptions, validate
from rashid.catalog import CatalogGraph
from rashid.model import Severity
from rashid.rules.conformance import declared_schema_uris
from rashid.schema import DEFAULT_SCHEMA_URI, bundled_schema
from scripts import vendor_spec_fixtures as vendor
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CATALOG = FIXTURES / "reference-catalog"
DRAFT_SCHEMA = FIXTURES / "draft-profile-schema.json"


def _schema_map_for_catalog(
    catalog: Path = CATALOG, draft_schema: Path = DRAFT_SCHEMA
) -> dict[str, dict]:
    """Use a test-only draft only when the declared URI has no bundled release."""
    graph = CatalogGraph.load(catalog)
    assert graph.root is not None, "reference catalog has no root; re-vendor fixtures"
    uris = declared_schema_uris(graph.root)
    assert len(uris) == 1, f"root declares {len(uris)} Portolan URIs: {uris}"
    uri = uris[0]
    if bundled_schema(uri) is not None:
        return {}
    schema = json.loads(draft_schema.read_bytes())
    assert schema["$id"].rstrip("#") == uri, (
        f"draft schema does not match {uri}; run scripts/vendor_spec_fixtures.py"
    )
    return {uri: schema}


def test_reference_catalog_metadata_pass_is_clean() -> None:
    report = validate(CATALOG, options=ValidationOptions(structural=False, schema=False))
    # The spec's reference catalog does not yet meet PORTO-CORE-088
    # (https://github.com/portolan-sdi/portolan-spec/pull/240); remove this allowance when it does.
    missing_text = [f for f in report.findings if f.rule_id == "PTL-AST-010"]
    assert all(
        f.severity is Severity.WARNING
        and f.message.endswith(("has no title", "has no description"))
        for f in missing_text
    )
    findings = [f for f in report.findings if f.rule_id != "PTL-AST-010"]
    blocking = [f for f in findings if f.severity is not Severity.INFO]
    assert blocking == [], [(f.rule_id, f.path) for f in blocking]
    # the only findings a conformant catalog may raise are the mirror INFOs
    assert {f.rule_id for f in findings} <= {"PTL-PRO-002"}
    assert report.passed


def test_single_cog_collection_models_its_raster_the_way_the_spec_prescribes() -> None:
    """raster/sample-cog is the single-COG shape PORTO-CORE-072 prescribes."""
    report = validate(CATALOG, options=ValidationOptions(structural=False, schema=False))
    assert [f for f in report.findings if f.rule_id == "PTL-COL-004"] == []


def test_reference_catalog_schema_pass_is_clean() -> None:
    report = validate(
        CATALOG,
        options=ValidationOptions(
            structural=False, schema=True, schema_map=_schema_map_for_catalog()
        ),
    )
    schema_findings = [f for f in report.findings if f.rule_id.startswith("PTL-SCH")]
    assert schema_findings == [], [(f.rule_id, f.path, f.message) for f in schema_findings]


@pytest.mark.parametrize("single_file", [False, True])
def test_canary_accepts_main_with_an_unreleased_profile(
    catalog: CatalogBuilder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, single_file: bool
) -> None:
    uri = "https://schemas.portolan-sdi.org/portolan/v99.0.0/schema.json"
    assert bundled_schema(uri) is None
    catalog.overrides["stac_extensions"] = [uri]
    catalog.collection("roads", stac_extensions=[uri]).item("segment")
    spec = tmp_path / "spec"
    shutil.copytree(catalog.write(), spec / vendor.CATALOG_SRC)
    schema_dir = spec / "stac/json-schema"
    schema_path = schema_dir / ("schema.json" if single_file else "v99.0.0/schema.json")
    schema_path.parent.mkdir(parents=True)
    released = bundled_schema(DEFAULT_SCHEMA_URI)
    assert released is not None
    draft = json.loads(json.dumps(released).replace(DEFAULT_SCHEMA_URI, uri))
    schema_path.write_text(json.dumps(draft))
    # The legacy fallback must select a numeric version, even beside older folders.
    for version in ("v0.2.0", "v0.10.0"):
        old = schema_dir / version / "schema.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps(released))
    fixtures = tmp_path / "fixtures"
    monkeypatch.setattr(vendor, "FIXTURES", fixtures)
    vendor._vendor_catalog(spec)
    vendor._vendor_draft_schema(spec)
    vendored_catalog = fixtures / "reference-catalog"
    draft_path = fixtures / "draft-profile-schema.json"
    schema_map = _schema_map_for_catalog(vendored_catalog, draft_path)
    options = ValidationOptions(
        structural=False, extensions=False, data=False, schema=True, schema_map=schema_map
    )

    report = validate(vendored_catalog, options=options)

    assert report.errors == []
    assert report.warnings == []
    assert draft_path.read_bytes() == schema_path.read_bytes()
    assert schema_map == {uri: draft}
    # A changed draft must change the verdict, rather than skip the schema pass.
    draft["required"] = ["draft_proof"]
    schema_map[uri] = draft
    errors = validate(vendored_catalog, options=options).errors
    assert len(errors) == 3
    assert all(
        error.rule_id == "PTL-SCH-001" and "draft_proof" in error.message for error in errors
    )


def test_released_reference_ignores_the_draft_fixture(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    assert _schema_map_for_catalog(catalog.write(), tmp_path / "absent.json") == {}


def test_unreleased_reference_rejects_a_mismatched_draft(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    catalog.overrides["stac_extensions"] = [
        "https://schemas.portolan-sdi.org/portolan/v99.0.0/schema.json"
    ]
    path = tmp_path / "draft.json"
    path.write_text(json.dumps({"$id": DEFAULT_SCHEMA_URI}))
    with pytest.raises(AssertionError, match="draft schema does not match"):
        _schema_map_for_catalog(catalog.write(), path)
