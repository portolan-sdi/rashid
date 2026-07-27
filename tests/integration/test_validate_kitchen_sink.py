"""A broken-everything tree produces exactly the expected finding set."""

from __future__ import annotations

import pytest

from rashid import validate
from tests.conftest import CatalogBuilder, mutate_json, rule_ids

pytestmark = pytest.mark.integration


def test_kitchen_sink(catalog: CatalogBuilder) -> None:
    collection = catalog.collection(
        "bad_layer",
        title="ns:bad_layer",  # namespace prefix -> TTL-002
        license="proprietary",  # LIC-003
        providers=[  # two hosts -> PRV-002; no producer -> PRV-001
            {"name": "A", "roles": ["host"]},
            {"name": "B", "roles": ["host"]},
        ],
        assets={
            "data": {  # no roles -> AST-001; s3 -> AST-002; raw digest -> AST-004
                "href": "s3://bucket/data.parquet",
                "type": "application/vnd.apache.parquet",
                "file:size": 10,
                "file:checksum": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            }
        },
    )
    collection.item("bad-item", properties={"datetime": "yesterday"})  # TMP-002
    root = catalog.write()

    (root / "bad_layer" / "AGENTS.md").unlink()  # FIL-001 (+ FIL-002 href dangling)
    mutate_json(
        root / "bad_layer" / "collection.json",
        lambda d: (
            d.pop("stac_extensions"),  # CNF-001
            d["extent"]["spatial"].__setitem__("bbox", [[4.0, 52.0, 6.0, 50.0]]),  # BBX-001
            d.__setitem__(
                "links", [link for link in d["links"] if link["rel"] != "parent"]
            ),  # LNK-001
        ),
    )
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {"rel": "self", "href": "./catalog.json", "type": "application/json"}
        ),  # LNK-005
    )

    report = validate(root)
    assert not report.passed
    assert rule_ids(report) == {
        "PTL-TTL-002",
        "PTL-LIC-003",
        "PTL-PRV-001",
        "PTL-PRV-002",
        "PTL-AST-001",
        "PTL-AST-002",
        "PTL-AST-004",
        "PTL-TMP-002",
        "PTL-FIL-001",
        "PTL-FIL-002",
        "PTL-CNF-001",
        "PTL-BBX-001",
        "PTL-LNK-001",
        "PTL-LNK-005",
        "PTL-VIZ-001",  # geospatial (item has geometry) but no thumbnail asset
    }
    # findings are stable-sorted by (path, rule_id)
    keys = [(f.path, f.rule_id) for f in report.findings]
    assert keys == sorted(keys)


def test_unparseable_collection_json(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    (root / "roads" / "collection.json").write_text("{broken", encoding="utf-8")
    report = validate(root)
    ids = rule_ids(report)
    assert "PTL-GEN-001" in ids
    # the dangling child link surfaces as a resolution failure, not a crash
    assert "PTL-LNK-006" in ids


def test_missing_root_catalog(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "README.md").write_text("# Not a catalog\n")
    report = validate(tmp_path)
    assert not report.passed
    assert [f.rule_id for f in report.findings] == ["PTL-GEN-000"]
