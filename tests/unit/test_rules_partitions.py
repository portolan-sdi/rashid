"""Tests for PTL-PRT-001 — a partitioned collection carries the extension fields."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reis import validate
from tests.conftest import CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit

_PARTITION_URI = "https://schemas.portolan-sdi.org/incubating/partition/v1.0.0/schema.json"


def _collection(root: Path) -> Path:
    return root / "roads" / "collection.json"


def _built(catalog: CatalogBuilder) -> Path:
    catalog.collection("roads").item("seg1")
    return catalog.write()


def _make_partitioned(data: dict[str, Any]) -> None:
    """Add every required partition field, so tests remove exactly one."""
    data["partition:scheme"] = "directory"
    data["partition:keys"] = [{"name": "province", "type": "string"}]
    data["partition:glob"] = "s3://bucket/roads/*/*.parquet"
    data["stac_extensions"] = [*data["stac_extensions"], _PARTITION_URI]


def test_non_partitioned_collection_is_clean(catalog: CatalogBuilder) -> None:
    report = validate(_built(catalog))
    assert findings_for(report, "PTL-PRT-001") == []


def test_fully_declared_partitioned_collection_is_clean(catalog: CatalogBuilder) -> None:
    root = _built(catalog)
    mutate_json(_collection(root), _make_partitioned)
    assert findings_for(validate(root), "PTL-PRT-001") == []


@pytest.mark.parametrize("field", ["partition:scheme", "partition:keys", "partition:glob"])
def test_missing_required_field_is_flagged(catalog: CatalogBuilder, field: str) -> None:
    root = _built(catalog)

    def mutate(data: dict[str, Any]) -> None:
        _make_partitioned(data)
        del data[field]

    mutate_json(_collection(root), mutate)
    findings = findings_for(validate(root), "PTL-PRT-001")
    assert len(findings) == 1
    assert findings[0].json_pointer == f"/{field}"


def test_glob_in_description_does_not_satisfy(catalog: CatalogBuilder) -> None:
    root = _built(catalog)

    def mutate(data: dict[str, Any]) -> None:
        _make_partitioned(data)
        del data["partition:glob"]
        data["description"] = "Roads across partitions at s3://bucket/roads/*.parquet"

    mutate_json(_collection(root), mutate)
    findings = findings_for(validate(root), "PTL-PRT-001")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/partition:glob"


def test_undeclared_extension_is_flagged(catalog: CatalogBuilder) -> None:
    root = _built(catalog)

    def mutate(data: dict[str, Any]) -> None:
        _make_partitioned(data)
        data["stac_extensions"] = [uri for uri in data["stac_extensions"] if uri != _PARTITION_URI]

    mutate_json(_collection(root), mutate)
    findings = findings_for(validate(root), "PTL-PRT-001")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/stac_extensions"


def test_extension_alone_triggers_field_requirements(catalog: CatalogBuilder) -> None:
    root = _built(catalog)
    mutate_json(
        _collection(root),
        lambda d: d.__setitem__("stac_extensions", [*d["stac_extensions"], _PARTITION_URI]),
    )
    findings = findings_for(validate(root), "PTL-PRT-001")
    assert {f.json_pointer for f in findings} == {
        "/partition:scheme",
        "/partition:keys",
        "/partition:glob",
    }


def test_empty_field_values_are_flagged(catalog: CatalogBuilder) -> None:
    root = _built(catalog)

    def mutate(data: dict[str, Any]) -> None:
        _make_partitioned(data)
        data["partition:scheme"] = "  "
        data["partition:keys"] = []

    mutate_json(_collection(root), mutate)
    findings = findings_for(validate(root), "PTL-PRT-001")
    assert {f.json_pointer for f in findings} == {"/partition:scheme", "/partition:keys"}
