"""The spec's reference catalog, vendored, must pass reis with zero errors.

This is the dogfood guard. ``portolan-spec``'s ``examples/catalog/reference`` is
the canonical example of a valid Portolan catalog; it is vendored here (JSON and
Markdown only) by ``scripts/vendor_spec_fixtures.py`` and validated by both the
metadata pass and the profile schema pass. The only findings allowed are the two
INFO mirror-canonical nudges. A regression here means reis has diverged from the
spec it validates — exactly the schema-URI drift these guards exist to catch.

The schema pass runs against the *vendored* schema matching the catalog's own
declared version, so it is hermetic: no network, and version-correct however many
schema versions are vendored.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reis import validate
from reis.catalog import CatalogGraph
from reis.model import Severity
from reis.rules.conformance import declared_schema_uris
from reis.schema import validator_from_schema

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CATALOG = FIXTURES / "reference-catalog"


def _schema_for_catalog() -> dict:
    """The vendored schema whose version the catalog declares."""
    graph = CatalogGraph.load(CATALOG)
    assert graph.root is not None, "reference catalog has no root; re-vendor fixtures"
    uris = declared_schema_uris(graph.root)
    assert len(uris) == 1, f"root declares {len(uris)} Portolan URIs: {uris}"
    version = uris[0].split("/portolan/")[1].split("/")[0]  # e.g. v0.1.0
    path = FIXTURES / "schema" / version / "schema.json"
    assert path.exists(), f"schema {version} not vendored; run scripts/vendor_spec_fixtures.py"
    return json.loads(path.read_text(encoding="utf-8"))


def test_reference_catalog_metadata_pass_is_clean() -> None:
    report = validate(CATALOG, structural=False, schema=False)
    blocking = [f for f in report.findings if f.severity is not Severity.INFO]
    assert blocking == [], [(f.rule_id, f.path) for f in blocking]
    # the only findings a conformant catalog may raise are the mirror INFOs
    assert {f.rule_id for f in report.findings} <= {"PTL-PRO-002"}
    assert report.passed


def test_single_cog_collection_models_its_raster_the_way_the_spec_prescribes() -> None:
    """raster/sample-cog is the single-COG shape PORTO-CORE-072 prescribes."""
    report = validate(CATALOG, structural=False, schema=False)
    assert [f for f in report.findings if f.rule_id == "PTL-COL-004"] == []


def test_reference_catalog_schema_pass_is_clean() -> None:
    local = validator_from_schema(_schema_for_catalog())
    report = validate(CATALOG, structural=False, schema=True, schema_validator=local)
    schema_findings = [f for f in report.findings if f.rule_id.startswith("PTL-SCH")]
    assert schema_findings == [], [(f.rule_id, f.path, f.message) for f in schema_findings]
