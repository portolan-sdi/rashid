"""Malformed spatial metadata is visible for assets and glob-only partitions."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from rashid.cli import main
from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare
from tests.integration.test_native_geoparquet import _WKB

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("raw", [b"not JSON", b"[]"])
@pytest.mark.parametrize("native", [False, True])
def test_malformed_geo_reports_once(tmp_path: Path, raw: bytes, native: bool) -> None:
    directory = _catalog(tmp_path, **{"partition:glob": "./*.parquet"})
    path = directory / "malformed.parquet"
    assets.write_geoparquet(path)
    table = pq.read_table(path).replace_schema_metadata({b"geo": raw})
    if native:
        geometry = pa.ExtensionArray.from_storage(_WKB(), table.column("geometry").combine_chunks())
        table = table.set_column(0, "geometry", geometry)
    pq.write_table(table, path)
    for declared in (False, True):
        if declared:
            _declare(directory, [path.name])
        result = CliRunner().invoke(
            main, ["check", "--json", "--data-scope", "local", str(tmp_path)]
        )
        assert result.exit_code in (0, 1), result.output
        findings = [
            finding
            for finding in json.loads(result.output)["findings"]
            if finding["rule_id"].startswith("PTL-DAT-")
        ]
        assert {finding["rule_id"] for finding in findings} == {"PTL-DAT-005"}
        assert len(findings) == 1 and findings[0]["severity"] == "info"
        assert "spatial metadata could not be read" in findings[0]["message"]
        assert "malformed.parquet" in findings[0]["message"]
