"""The file owner reuses handles, and result builds depend only on their sources."""

from __future__ import annotations

import json
import random
import subprocess
import sys
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
    assert counts["parquet.opens"] == 3
    assert {f["severity"] for f in ordering} == ({"info"} if nested else {"error"})
    assert not _findings(tmp_path, "PTL-DAT-007")


@pytest.mark.parametrize("seed", range(20))
def test_result_build_is_a_function_of_sources(seed: int) -> None:
    """Rebuilding and extending a built accumulator equals a fresh derivation."""
    from rashid.data import DataResults

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
    source = DataResults()
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
    fresh = DataResults()
    fresh.extend(partitions, partition_total=20)
    fresh.extend([*assets_source, extra])
    assert source.build() == fresh.build() == [*first, extra]


@pytest.mark.skipif(sys.platform == "win32", reason="Unix descriptor limit")
def test_many_partitions_open_once_without_exhausting_handles(tmp_path: Path) -> None:
    """A real CLI process with fewer descriptors than partition files checks all of them."""
    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet"})
    for i in range(100):
        assets.write_geoparquet(directory / f"{i:03}.parquet", version="1.0.0")
    code = (
        "import resource; resource.setrlimit(resource.RLIMIT_NOFILE, (80, 80)); "
        "from rashid.cli import main; main()"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "--timings",
            "check",
            "--json",
            "--data-scope",
            "local",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    findings = [f for f in report["findings"] if f["rule_id"] == "PTL-DAT-012"]
    assert len(findings) == 1 and findings[0]["message"].startswith("100 of 100 partition files")
    assert report["meta"]["counters"]["parquet.opens"] == 100


def test_partition_open_failure_reports_unavailable_data(tmp_path: Path) -> None:
    """A failed open must be visible even when no declared asset owns the bytes."""
    from click.testing import CliRunner

    from rashid.cli import main

    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet"})
    (directory / "broken.parquet").write_bytes(b"not a Parquet file")
    result = CliRunner().invoke(main, ["check", "--json", "--data-scope", "local", str(tmp_path)])
    assert result.exit_code == 1, result.output
    findings = [f for f in json.loads(result.output)["findings"] if f["rule_id"] == "PTL-DAT-000"]
    assert len(findings) == 1 and findings[0]["severity"] == "warning"
    assert "broken.parquet" in findings[0]["message"]
    assert findings[0]["json_pointer"] == "/partition:glob"


@pytest.mark.parametrize("readable", [False, True])
def test_declared_corrupt_partitions_use_the_full_population(
    tmp_path: Path, readable: bool
) -> None:
    from click.testing import CliRunner

    from rashid.cli import main

    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet"})
    names = ["first.parquet", "second.parquet"]
    for name in names:
        (directory / name).write_bytes(b"not a Parquet file")
    if readable:
        names.append("readable.parquet")
        assets.write_geoparquet(directory / names[-1])
    _declare(directory, names)
    result = CliRunner().invoke(main, ["check", "--json", "--data-scope", "local", str(tmp_path)])
    assert result.exit_code == 1, result.output
    findings = [
        finding
        for finding in json.loads(result.output)["findings"]
        if finding["rule_id"] == "PTL-DAT-000"
    ]
    assert len(findings) == 1
    assert findings[0]["message"].startswith(f"2 of {len(names)} partition files fail this check;")
