"""The file owner reuses handles, and result builds depend only on their sources."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rashid import _trace
from rashid.data import DataDefect
from rashid.model import Severity
from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare, _findings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("nested", [False, True])
def test_partition_handles_and_covering_policies(tmp_path: Path, nested: bool) -> None:
    """Footer statistics support deep leaves; row reads require a shallow struct."""
    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet", "proj:epsg": 3857})
    for name in ("a", "b"):
        path = directory / (name + ".parquet")
        assets.write_geoparquet(path, points=assets.scattered_points(200), row_group_size=200)
        if nested:
            table = pq.read_table(path)
            geo = json.loads(table.schema.metadata[b"geo"])
            bbox = table.column("bbox").combine_chunks()
            table = table.drop(["bbox"]).append_column(
                "outer", pa.StructArray.from_arrays([bbox], names=["bbox"])
            )
            geo["columns"]["geometry"]["covering"]["bbox"] = {
                c: ["outer", "bbox", c] for c in ("xmin", "ymin", "xmax", "ymax")
            }
            pq.write_table(table.replace_schema_metadata({b"geo": json.dumps(geo).encode()}), path)
    _declare(directory, ["a.parquet"])
    with _trace.counting() as counts:
        ordering = _findings(tmp_path, "PTL-DAT-006")
    assert counts["parquet.opens"] == 2
    assert {f["severity"] for f in ordering} == ({"info"} if nested else {"error"})
    assert not _findings(tmp_path, "PTL-DAT-007")


@pytest.mark.parametrize("seed", range(20))
def test_result_build_is_a_function_of_sources(seed: int) -> None:
    """Rebuilding and extending a built accumulator equals a fresh derivation."""
    from rashid.data import _DataResults

    rng = random.Random(seed)
    assets_source = [
        DataDefect(
            "PTL-DAT-005",
            Severity.WARNING,
            f"CRS {rng.randrange(3)}",
            str(i),
            json_pointer="/proj:epsg",
        )
        for i in range(rng.randrange(1, 15))
    ]
    partitions = [
        DataDefect("PTL-DAT-007", rng.choice(list(Severity)), f"file {i}", str(i))
        for i in range(rng.randrange(1, 15))
    ]
    source = _DataResults()
    source.extend(partitions, partition_total=20)
    source.extend(assets_source)
    first = source.build()
    assert source.build() == first
    asset_unique = list(dict.fromkeys(d.message for d in assets_source))
    assert [d.message for d in first if d.rule_id == "PTL-DAT-005"] == asset_unique
    for severity in {d.severity for d in partitions}:
        members = [d for d in partitions if d.severity == severity]
        result = next(d for d in first if d.rule_id == "PTL-DAT-007" and d.severity == severity)
        assert result.json_pointer == "/partition:glob"
        assert result.message == (
            members[0].message
            if len(members) == 1
            else f"{len(members)} of 20 partition files fail this check; e.g. {members[0].message}"
        )
    extra = DataDefect("PTL-DAT-005", Severity.WARNING, "asset", "extra")
    source.extend([extra])
    fresh = _DataResults()
    fresh.extend(partitions, partition_total=20)
    fresh.extend([*assets_source, extra])
    assert source.build() == fresh.build() == [*first, extra]
