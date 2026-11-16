"""Tests for PTL-PRT-001 — a partitioned collection carries the extension fields."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rashid import validate
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


@pytest.mark.parametrize(
    "glob",
    [
        "http://example.org/*.parquet",
        "https://example.org/*.parquet",
        "HTTPS://example.org/*.parquet",
        "https://[invalid",
        "./parts/*.parquet",
        "parts/*.parquet",
        "/data/parts/*.parquet",
        "C:/data/*.parquet",
        "s3://",
        "gs:///data/*.parquet",
        "az:container/*.parquet",
        "year:2024/*.parquet",
        "abfs:container/*.parquet",
        "s3://user@/data/*.parquet",
        "s3://:80/data/*.parquet",
        "s3://bucket:bad/data/*.parquet",
        "s3://bucket:99999/data/*.parquet",
        "file:parts/*.parquet",
        "file:",
        # A file URL names a path on the publisher's machine; no client elsewhere lists it.
        "file:///data/*.parquet",
        "file://host/data/*.parquet",
        # A bucket with no key pattern names nothing to match.
        "s3://bucket/",
        "s3://bucket",
    ],
)
def test_unlistable_glob_is_rejected(catalog: CatalogBuilder, glob: str) -> None:
    root = _built(catalog)
    mutate_json(
        _collection(root),
        lambda d: d.__setitem__("partition:glob", glob),
    )
    findings = findings_for(validate(root), "PTL-PRT-002")
    assert len(findings) == 1
    assert findings[0].severity.value == "error"
    assert findings[0].json_pointer == "/partition:glob"
    assert findings[0].fix_hint == "use the absolute bucket URL, e.g. 's3://bucket/data/*.parquet'"


@pytest.mark.parametrize(
    "glob",
    [
        "s3://bucket/*.parquet",
        "S3://bucket/*.parquet",
        # ? is a glob wildcard and # a key character, not a query or a fragment.
        "s3://bucket/data/part-?.parquet",
        "s3://bucket/a#b/*.parquet",
        "gs://bucket/*.parquet",
        "az://container/*.parquet",
        " s3://bucket/*.parquet",
        None,
        123,
        "  ",
    ],
)
def test_recommended_or_missing_glob_is_permitted(catalog: CatalogBuilder, glob: object) -> None:
    root = _built(catalog)
    mutate_json(_collection(root), lambda d: d.__setitem__("partition:glob", glob))
    assert findings_for(validate(root), "PTL-PRT-002") == []


@pytest.mark.parametrize("glob", ["hf://datasets/org/data/*.parquet", "abfs://c@a/*.parquet"])
def test_other_absolute_scheme_is_a_warning(catalog: CatalogBuilder, glob: str) -> None:
    root = _built(catalog)
    mutate_json(_collection(root), lambda d: d.__setitem__("partition:glob", glob))
    findings = findings_for(validate(root), "PTL-PRT-002")
    assert len(findings) == 1
    assert findings[0].severity.value == "warning"
    assert findings[0].json_pointer == "/partition:glob"
    assert findings[0].actual == glob


@pytest.mark.parametrize("version", ["v1.0.0", "v1.2.3"])
def test_a_v1_partition_extension_passes(catalog: CatalogBuilder, version: str) -> None:
    root = _built(catalog)
    uri = _PARTITION_URI.replace("v1.0.0", version)

    def declare(data: dict[str, Any]) -> None:
        _make_partitioned(data)
        data["stac_extensions"] = [u for u in data["stac_extensions"] if u != _PARTITION_URI]
        data["stac_extensions"].append(uri)

    mutate_json(_collection(root), declare)
    report = validate(root)
    assert findings_for(report, "PTL-PRT-001") == []
    assert findings_for(report, "PTL-CNF-004") == []


@pytest.mark.parametrize("version", ["v2.0.0", "v0.9.0"])
def test_a_partition_extension_other_than_v1_is_an_error(
    catalog: CatalogBuilder, version: str
) -> None:
    """FMT-017 requires v1.0.0; PTL-PRT-001 reports it, and PTL-CNF-004 stays quiet."""
    root = _built(catalog)
    uri = _PARTITION_URI.replace("v1.0.0", version)

    def declare(data: dict[str, Any]) -> None:
        _make_partitioned(data)
        data["stac_extensions"][-1] = uri

    mutate_json(_collection(root), declare)
    report = validate(root)
    findings = findings_for(report, "PTL-PRT-001")
    assert [(f.severity.value, f.actual, f.expected) for f in findings] == [
        ("error", uri, _PARTITION_URI)
    ]
    assert findings[0].json_pointer.startswith("/stac_extensions/")
    assert findings_for(report, "PTL-CNF-004") == []


@pytest.mark.parametrize("target", ["catalog", "item"])
def test_a_partition_extension_other_than_v1_off_a_collection_gets_cnf_004(
    catalog: CatalogBuilder, target: str
) -> None:
    """PTL-PRT-001 checks collections only, so CNF-004 keeps the version elsewhere."""
    root = _built(catalog)
    path = root / "catalog.json" if target == "catalog" else root / "roads" / "seg1" / "seg1.json"
    uri = _PARTITION_URI.replace("v1.0.0", "v2.0.0")
    mutate_json(path, lambda d: d.setdefault("stac_extensions", []).append(uri))
    findings = findings_for(validate(root), "PTL-CNF-004")
    assert [f.actual for f in findings] == [uri]
