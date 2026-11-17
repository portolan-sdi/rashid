"""Small helpers in ``rashid.rules._common``."""

from __future__ import annotations

import pytest

from rashid.rules._common import media_type_essence, names_a_geometry_column

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
    ],
)
def test_names_a_geometry_column(columns: list[dict[str, str]], found: bool) -> None:
    assert names_a_geometry_column(columns) is found
