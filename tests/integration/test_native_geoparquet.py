"""Public CLI regressions for the GeoParquet and file owner track (#266)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.integration import _data_assets as assets
from tests.integration._owner_catalog import _catalog, _declare, _findings

pytestmark = pytest.mark.integration


class _WKB(pa.ExtensionType):
    """A real GeoArrow storage type, which Arrow writes as native GEOMETRY."""

    def __init__(self, spherical: bool = False) -> None:
        self.spherical = spherical
        super().__init__(pa.binary(), "geoarrow.wkb")

    def __arrow_ext_serialize__(self) -> bytes:
        return b'{"edges":"spherical"}' if self.spherical else b"{}"

    @classmethod
    def __arrow_ext_deserialize__(cls, _storage: object, _serialized: bytes) -> _WKB:
        return cls()


@pytest.mark.parametrize("spherical", [False, True])
def test_native_geometry_is_accepted_and_still_checks_groups(
    tmp_path: Path, spherical: bool
) -> None:
    """#200: native geometry is spatial even when the GeoParquet key is absent.

    PORTO-FMT-055 makes the native-geo file without a ``geo`` key the spec's
    third accepted form, so the missing key is no longer faulted (the native
    ``crs`` rules take its place); the storage rules still run on the file.
    """
    directory = _catalog(tmp_path)
    path = directory / "native.parquet"
    assets.write_geoparquet(path, points=assets.interleaved_points(), covering=False)
    table = pq.read_table(path)
    geo = json.loads(table.schema.metadata[b"geo"])
    geo["version"] = "2.0.0"
    geometry = pa.ExtensionArray.from_storage(
        _WKB(spherical), table.column("geometry").combine_chunks()
    )
    table = table.set_column(0, "geometry", geometry).replace_schema_metadata(None)
    pq.write_table(table, path, row_group_size=2)
    assert pq.ParquetFile(path).schema.column(0).logical_type.type == (
        "GEOGRAPHY" if spherical else "GEOMETRY"
    )
    _declare(directory, [path.name])
    assert not _findings(tmp_path, "PTL-DAT-012"), "the missing geo key is not a version error"
    assert not _findings(tmp_path, "PTL-DAT-018"), "an absent native crs is OGC:CRS84"
    if not spherical:
        assert any(f["severity"] == "error" for f in _findings(tmp_path, "PTL-DAT-006"))
    assert [f["severity"] for f in _findings(tmp_path, "PTL-DAT-007")] == (
        ["error"] if spherical else ["warning"]
    )
    assert not _findings(tmp_path, "PTL-DAT-015")
    assert len(_findings(tmp_path, "PTL-DAT-017")) == 1
    pq.write_table(
        table.replace_schema_metadata({b"geo": json.dumps(geo).encode()}), path, row_group_size=2
    )
    assert not _findings(tmp_path, "PTL-DAT-012")
    assert not _findings(tmp_path, "PTL-DAT-015")
    assert len(_findings(tmp_path, "PTL-DAT-017")) == 1
    pq.write_table(pa.concat_tables([table] * 15_001), path, row_group_size=150_010)
    assert [f["severity"] for f in _findings(tmp_path, "PTL-DAT-008")] == ["error"]
    plain = pa.table({"value": [1, 2]})
    pq.write_table(plain, path)
    assert not _findings(tmp_path, "PTL-DAT-012")


@pytest.mark.parametrize("scope", ["all", "local"])
@pytest.mark.parametrize("spherical", [False, True])
def test_collection_extent_with_native_geometry_without_geo(
    tmp_path: Path, scope: str, spherical: bool
) -> None:
    from tests.integration.test_href_behavior import _check

    directory = _catalog(tmp_path, extent={"spatial": {"bbox": [[0, 0, 2, 1]]}})
    for start, name in ((0, "west"), (1, "east")):
        assets.write_geoparquet(directory / f"{name}.parquet", points=[(start, 0), (start + 1, 1)])
    east = directory / "east.parquet"
    table = pq.read_table(east)
    geometry = pa.ExtensionArray.from_storage(
        _WKB(spherical), table.column("geometry").combine_chunks()
    )
    pq.write_table(table.set_column(0, "geometry", geometry).replace_schema_metadata(None), east)
    _declare(directory, ["west.parquet", "east.parquet"])

    report = _check(tmp_path, "--data-scope", scope)
    assert not [f for f in report["findings"] if f["rule_id"] == "PTL-DAT-005"]
