"""PTL-DAT-006 tells the producer which row order passes (rashid#339).

The fixture holds the row boxes of the wallonie-spw ``mobilite/nationales``
file in file order: 3,255 national roads in one row group, written by the
default ``gpio sort str``, in EPSG:31370 metres rounded to 1 m. The row check
scores it 0.52, as rashid 0.4.0 did on the published file. The verdict stays
as the spec sets it, a warning. The finding now carries a fix hint that names
a curve sort, which the spatial-order calibration found to pass (Hilbert
median 0.90). ``gpio sort hilbert`` scored 0.68 on this file.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("pyarrow")

import pyarrow.parquet as pq

import rashid_core.data as data_pass
from rashid_core.catalog import Node
from rashid_core.data._results import DataDefect, DataResults
from rashid_core.data.formats import geoparquet
from rashid_core.data.formats._files import AssetFiles
from rashid_core.data.reader import Locator
from rashid_core.ids import DAT_ORDERING
from rashid_core.model import Severity
from tests.integration import _data_assets as assets

pytestmark = pytest.mark.integration

_NATIONALES = Path(__file__).resolve().parent.parent / "fixtures" / "nationales-row-boxes.json.gz"


def _ordering(path: Path) -> list[DataDefect]:
    defects = geoparquet.check_geoparquet(
        "data", AssetFiles(Locator(is_remote=False, source=str(path)))
    )
    return [d for d in defects if d.rule_id == DAT_ORDERING]


def _assert_names_a_passing_order(defect: DataDefect) -> None:
    assert defect.fix_hint is not None
    assert "Hilbert" in defect.fix_hint
    assert "gpio sort hilbert" in defect.fix_hint


def test_real_single_row_group_warns_with_a_hint(tmp_path: Path) -> None:
    """One strip of `gpio sort str` leaves long road boxes in every chunk."""
    boxes = [tuple(b) for b in json.loads(gzip.decompress(_NATIONALES.read_bytes()))]
    path = tmp_path / "nationales.parquet"
    centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
    assets.write_geoparquet(path, points=centres, bboxes=boxes, row_group_size=len(boxes))
    assert pq.ParquetFile(path).metadata.num_row_groups == 1

    defects = _ordering(path)

    assert [d.severity for d in defects] == [Severity.WARNING]
    assert "3255 rows in 1 row group" in defects[0].message
    assert "(efficiency 0.52, under the 0.65 bar)" in defects[0].message
    _assert_names_a_passing_order(defects[0])


def test_footer_check_failure_carries_the_hint(tmp_path: Path) -> None:
    """Many row groups with poor locality still warn, now with the hint."""
    path = tmp_path / "interleaved.parquet"
    assets.write_geoparquet(path, points=assets.interleaved_points())
    assert pq.ParquetFile(path).metadata.num_row_groups == geoparquet._MIN_ORDERING_ROW_GROUPS

    defects = _ordering(path)

    assert [d.severity for d in defects] == [Severity.WARNING]
    assert "across its 8 row groups" in defects[0].message
    _assert_names_a_passing_order(defects[0])


def test_unevaluated_file_gets_no_hint(tmp_path: Path) -> None:
    """Under the row floor there is no verdict, so there is nothing to fix."""
    path = tmp_path / "few.parquet"
    assets.write_geoparquet(path, points=assets.ordered_points())

    defects = _ordering(path)

    assert [d.severity for d in defects] == [Severity.INFO]
    assert defects[0].fix_hint is None


def test_hint_reaches_the_finding_and_the_partition_summary() -> None:
    """The hint survives the partition roll-up and the move to a Finding."""
    defect = DataDefect(DAT_ORDERING, Severity.WARNING, "part rows", "data", fix_hint="sort")
    results = DataResults()
    results.extend([defect, defect], partition_total=4)
    (rolled,) = results.build()
    assert rolled.fix_hint == "sort"

    node = Node(
        path=PurePosixPath("layer/collection.json"),
        abs_path=Path("/nowhere/collection.json"),
        kind="collection",
        id="layer",
        data={"type": "Collection", "assets": {"data": {}}},
    )
    (finding,) = data_pass._to_findings(node, [rolled])
    assert finding.fix_hint == "sort"
