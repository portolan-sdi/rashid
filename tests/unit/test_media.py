"""Media types and STAC geometry helpers."""

from __future__ import annotations

import pytest

from rashid_core._media import media_type_essence
from rashid_core._stac import column_geometry, names_a_geometry_column

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("value", "essence"),
    [
        ("text/html", "text/html"),
        ("Text/HTML; charset=utf-8", "text/html"),
        (" application/json ;profile=x", "application/json"),
        (None, None),
        (3, None),
    ],
)
def test_media_type_essence(value: object, essence: str | None) -> None:
    assert media_type_essence(value) == essence


@pytest.mark.parametrize(
    ("columns", "found"),
    [
        ([{"name": "geography"}], True),
        ([{"name": "GEOG"}], True),
        ([{"name": "shape", "type": "GEOGRAPHY(OGC:CRS84)"}], True),
        ([{"name": "geom"}], True),
        ([{"name": "geographic_region", "type": "string"}], False),
        # Esri and GDAL/OGR default geometry column names (#334).
        ([{"name": "Shape", "type": "binary"}], True),
        ([{"name": "wkb_geometry", "type": "binary"}], True),
        ([{"name": "WKB_GEOM"}], True),
        ([{"name": "shape_area", "type": "double"}], False),
        ([{"name": "footprint", "type": "binary"}], False),
    ],
)
def test_names_a_geometry_column(columns: list[dict[str, str]], found: bool) -> None:
    assert names_a_geometry_column(columns) is found


@pytest.mark.parametrize(
    ("columns", "evidence"),
    [
        ([{"name": "Shape", "type": "binary"}], True),
        ([{"name": "wkb_geometry", "type": "binary"}], True),
        ([{"name": "id", "type": "int64"}, {"name": "geometry", "type": "binary"}], True),
        # A byte column can hold WKB geometry, so it leaves the question open.
        ([{"name": "footprint", "type": "binary"}], None),
        ([{"name": "footprint", "type": "large_binary"}], None),
        ([{"name": "footprint", "type": "BYTE_ARRAY"}], None),
        ([{"name": "footprint", "type": "blob"}], None),
        ([{"name": "id", "type": "int64"}, {"name": "year", "type": "string"}], False),
        ([{"name": "id"}], False),
        ([], False),
    ],
)
def test_column_geometry(columns: list[dict[str, str]], evidence: bool | None) -> None:
    """True is a geometry column, False excludes geometry, None cannot tell."""
    assert column_geometry(columns) is evidence
