"""rashid's schema-URI pattern must match every vendored profile schema's ``$id``.

The keystone regression guard for spec drift. ``SCHEMA_URI_PATTERN`` (behind
``PTL-CNF-001``) encodes what rashid considers a valid Portolan schema URI; the
authority for that shape is each published schema's own ``$id``. When the spec
renamespaced its schema host, this invariant broke silently and rashid began
rejecting every conformant catalog. Asserting it over every vendored version
turns the next such move into a failing test the moment the schema is
re-vendored — and does so version-agnostically: a new ``vX.Y.Z`` is covered as
soon as it lands in the fixtures, no code change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rashid.rules.conformance import SCHEMA_URI_PATTERN
from rashid.schema import DEFAULT_SCHEMA_URI

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _version_key(schema_path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in schema_path.parent.name.lstrip("v").split("."))


SCHEMAS = sorted((FIXTURES / "schema").glob("v*/schema.json"), key=_version_key)


def _schema_id(schema_path: Path) -> str:
    # $id carries a trailing '#'; the declared stac_extensions URI omits it.
    return json.loads(schema_path.read_text(encoding="utf-8"))["$id"].rstrip("#")


def test_schemas_are_vendored() -> None:
    assert SCHEMAS, "no vendored schema; run scripts/vendor_spec_fixtures.py"


@pytest.mark.parametrize("schema_path", SCHEMAS, ids=lambda p: p.parent.name)
def test_pattern_matches_published_schema_id(schema_path: Path) -> None:
    schema_id = _schema_id(schema_path)
    assert SCHEMA_URI_PATTERN.match(schema_id), (
        f"rashid SCHEMA_URI_PATTERN rejects the spec's own $id {schema_id!r}; "
        "rashid has drifted from the published schema URI"
    )


def test_default_schema_uri_tracks_latest_vendored() -> None:
    """The last-resort fallback must not lag the newest published schema."""
    latest = _schema_id(SCHEMAS[-1])
    assert DEFAULT_SCHEMA_URI == latest, (
        f"DEFAULT_SCHEMA_URI {DEFAULT_SCHEMA_URI!r} is not the latest vendored "
        f"schema $id {latest!r}; bump it when adopting a new schema version"
    )
