"""Malformed spatial metadata is visible for assets and glob-only partitions."""

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare, _findings

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("raw", [b"not JSON", b"[]"])
def test_malformed_geo_reports_once(tmp_path: Path, raw: bytes) -> None:
    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet"})
    path = directory / "malformed.parquet"
    assets.write_geoparquet(path)
    table = pq.read_table(path).replace_schema_metadata({b"geo": raw})
    pq.write_table(table, path)
    for declared in (False, True):
        if declared:
            _declare(directory, [path.name])
        findings = _findings(tmp_path, "PTL-DAT-005")
        assert len(findings) == 1 and findings[0]["severity"] == "info"
        assert "spatial metadata could not be read" in findings[0]["message"]
        assert "malformed.parquet" in findings[0]["message"]
