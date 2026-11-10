"""Item mirror membership and field comparisons."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import shapely
from shapely import from_wkb
from shapely.errors import ShapelyError
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from rashid.catalog import CatalogGraph, Node
from rashid.data import DataDefect
from rashid.data._files import AssetFiles
from rashid.data.geoparquet import bbox_column_values, bbox_struct, geo_metadata
from rashid.data.spatial import as_bbox
from rashid.model import Severity
from rashid.registry import (
    DAT_MIRROR,
)

# --- item mirrors -----------------------------------------------------------


# Coordinate agreement tolerance, in degrees, for the mirror comparison. Item
# JSON and WKB both carry float64, so the only divergence a conformant pair
# shows is decimal formatting on the JSON side: a coordinate written to six
# decimals lands within 5e-7 of the value the mirror stores. 1e-6 degrees is
# about 0.11 m at the equator, well under any edit a publisher makes to a
# footprint.
_MIRROR_COORD_TOL = 1e-6

# Timestamp agreement tolerance. A STAC datetime is an RFC 3339 string with
# arbitrary fractional precision, while a Parquet timestamp column stores one
# fixed unit, so a conformant mirror rounds the fraction away. A mirror left
# behind by an edited item is off by minutes at least.
_MIRROR_TIME_TOL = timedelta(seconds=1)

# RFC 3339: fromisoformat on Python 3.10 rejects the 'Z' suffix and any
# fraction that is not three or six digits, both of which the grammar allows.
_RFC3339 = re.compile(
    r"(?P<stamp>\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<offset>[Zz]|[+-]\d{2}:?\d{2})?$"
)


@dataclass(frozen=True)
class _MirrorRows:
    """The mirror columns the comparison reads, one entry per row.

    ``geometry``, ``datetimes``, and ``bboxes`` are None when the file carries
    no such column, which is not the same as a column full of nulls.
    """

    ids: list[Any]
    geometry: list[Any] | None
    datetimes: list[Any] | None
    bboxes: list[Any] | None


def check_mirror(  # noqa: PLR0911 - guard clauses follow the documented skip conditions
    node: Node, key: str, files: AssetFiles, graph: CatalogGraph | None
) -> list[DataDefect]:
    """A mirror MUST reproduce the collection's items (PORTO-FMT-042).

    formats.md, Raster § Item mirror: "the file MUST reproduce the
    collection's items exactly at publish time — one row per item, each row
    carrying that item's fields." Five comparisons stand in for that: the row
    count against the item count, the two id sets against each other, and the
    geometry, datetime, and bbox of every matched row against the item it
    names. Geometry and datetime are what a client queries the mirror on, and
    they are what goes stale when an item is edited and the mirror is not
    regenerated.

    Fidelity stops short of every field. stac-geoparquet is lossy by design in
    places — its ``docs/drawbacks.md`` names the
    defined-versus-undefined-versus-null distinction among item properties — so
    a field-by-field equality rule would fault conformant files.

    Skipped when the graph is unavailable, when the node is not a collection,
    when the collection's items carry no ids to join on, or when it has no
    items at all. That last case is a structural defect rather than a
    row-level one, and ``PTL-COL-005`` owns it: a mirror on an item-less
    collection is reported there, from metadata alone, without the data extra
    and without reading the Parquet bytes.
    """
    if graph is None or node.kind != "collection":
        return []
    items = graph.items_of(node)
    if not items:
        return []  # PTL-COL-005 owns the item-less collection
    identified = {item.id: item for item in items if isinstance(item.id, str) and item.id}
    if not identified:
        return []  # unidentified items; PTL-STR/PTL-GEN own that gap

    try:
        parquet = files.parquet()
    except Exception:  # unreadable Parquet: format/checksum checks own it
        return []

    if "id" not in parquet.schema_arrow.names:
        return [
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' has no 'id' column, so its rows cannot be matched"
                " to the collection's items",
                key,
            )
        ]

    rows = _read_mirror_rows(parquet)
    if rows is None:
        return []
    defects = _mirror_membership_defects(key, identified, rows)
    defects.extend(_mirror_field_defects(key, identified, rows))
    return defects


def _mirror_membership_defects(
    key: str, identified: dict[str, Node], rows: _MirrorRows
) -> list[DataDefect]:
    """One row per item, and the two id sets equal.

    The counts are compared as counts because the id sets are sets: two rows
    sharing one id satisfy set equality, and a mirror that repeats an item is
    not one row per item.
    """
    defects: list[DataDefect] = []
    if len(rows.ids) != len(identified):
        defects.append(
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' holds {len(rows.ids)} row(s) for {len(identified)} "
                "item(s); the mirror must carry one row per item",
                key,
            )
        )
    present = {value for value in rows.ids if isinstance(value, str)}
    missing = sorted(set(identified) - present)
    extra = sorted(present - set(identified))
    parts = []
    if missing:
        parts.append(f"{len(missing)} item(s) absent from the mirror ({_sample(missing)})")
    if extra:
        parts.append(f"{len(extra)} mirror row(s) with no item ({_sample(extra)})")
    if parts:
        defects.append(
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' disagrees with the collection's items: " + ", ".join(parts),
                key,
            )
        )
    return defects


def _mirror_field_defects(
    key: str, identified: dict[str, Node], rows: _MirrorRows
) -> list[DataDefect]:
    """Geometry, datetime, and bbox of each matched row against its item.

    Only ids both sides carry are compared; an id on one side alone is the
    membership defect's business. A column the file lacks is left to the rule
    that owns it: the GeoParquet rules own a file with no geometry, and
    ``PTL-DAT-007`` owns one with no bbox covering column. A missing datetime
    column has no other owner, so it is reported here.
    """
    index = {value: i for i, value in enumerate(rows.ids) if isinstance(value, str)}
    off: dict[str, list[str]] = {"geometry": [], "datetime": [], "bbox": []}
    for item_id in sorted(set(identified) & set(index)):
        item, i = identified[item_id], index[item_id]
        if rows.geometry is not None and not _geometry_agrees(item, rows.geometry[i]):
            off["geometry"].append(item_id)
        if rows.datetimes is not None and not _datetime_agrees(item, rows.datetimes[i]):
            off["datetime"].append(item_id)
        if rows.bboxes is not None and not _bbox_agrees(item, rows.bboxes[i]):
            off["bbox"].append(item_id)
    defects = [
        DataDefect(
            DAT_MIRROR,
            Severity.ERROR,
            f"mirror asset '{key}' {field} disagrees with the item it names for "
            f"{len(ids)} item(s) ({_sample(ids)})",
            key,
        )
        for field, ids in off.items()
        if ids
    ]
    if rows.datetimes is None and any(_item_datetime(item) for item in identified.values()):
        defects.append(
            DataDefect(
                DAT_MIRROR,
                Severity.ERROR,
                f"mirror asset '{key}' has no 'datetime' column, so it does not carry the "
                "items' datetimes",
                key,
            )
        )
    return defects


def _read_mirror_rows(parquet: Any) -> _MirrorRows | None:
    """Read id, geometry, datetime, and bbox in one pass over the file."""
    names = parquet.schema_arrow.names
    metadata = geo_metadata(parquet)
    geo = metadata if isinstance(metadata, dict) else {}
    primary = geo.get("primary_column")
    geometry_name = primary if isinstance(primary, str) and primary in names else None
    if geometry_name is None and "geometry" in names:
        geometry_name = "geometry"
    datetime_name = "datetime" if "datetime" in names else None
    bbox = bbox_struct(parquet, geo)
    wanted = [geometry_name, datetime_name, bbox[0] if bbox is not None else None]
    try:
        table = parquet.read(columns=["id", *(name for name in wanted if name is not None)])
    except Exception:  # unreadable Parquet: format/checksum checks own it
        return None
    return _MirrorRows(
        ids=table.column("id").to_pylist(),
        geometry=table.column(geometry_name).to_pylist() if geometry_name else None,
        datetimes=table.column(datetime_name).to_pylist() if datetime_name else None,
        bboxes=bbox_column_values(table, bbox) if bbox is not None else None,
    )


def _geometry_agrees(item: Node, blob: Any) -> bool:
    """Does one mirror row's geometry reproduce the item's?

    Both sides are built as shapely geometries, the mirror's from WKB and the
    item's from GeoJSON, and compared with ``shapely.equals_exact`` within
    ``_MIRROR_COORD_TOL``. That comparison is structural: it holds only when
    the two agree on type, on part and ring nesting, and on vertex order, so a
    polygon whose rings were redrawn over the same points is drift rather than
    a match. The tolerance is there because both sides hold float64, so exact
    equality would fault a mirror built from full-precision sources against
    item JSON written to a fixed number of decimals. A blob shapely cannot
    decode is left alone, because an encoding it does not know is not evidence
    of drift; a null geometry against an item that has one is drift.
    """
    declared = _shape(item.data.get("geometry"))
    if declared is None:
        return True  # a null or unreadable item geometry is nothing to compare against
    if blob is None:
        return False
    if not isinstance(blob, bytes):
        return True
    actual = _from_wkb(blob)
    if actual is None:
        return True
    return bool(shapely.equals_exact(declared, actual, tolerance=_MIRROR_COORD_TOL))


def _shape(geometry: Any) -> BaseGeometry | None:
    """A GeoJSON geometry mapping as a shapely geometry, or None if unusable.

    An empty geometry counts as unusable: a member-less collection or a
    coordinate-less point carries nothing to compare a mirror row against.
    """
    if not isinstance(geometry, dict):
        return None
    try:
        built = shape(geometry)
    except (ShapelyError, AttributeError, KeyError, TypeError, ValueError):
        return None
    return None if built.is_empty else built


def _from_wkb(blob: bytes) -> BaseGeometry | None:
    """A WKB blob as a shapely geometry, or None if shapely cannot read it."""
    try:
        return from_wkb(blob)
    except (ShapelyError, TypeError, ValueError):
        return None


def _datetime_agrees(item: Node, value: Any) -> bool:
    """Does one mirror row's timestamp reproduce the item's ``datetime``?

    Both sides are normalized to UTC before the comparison, so a mirror
    written in local time with its offset still agrees with a ``Z`` item. An
    item whose ``datetime`` is null carries the interval in
    ``start_datetime``/``end_datetime`` instead and has nothing to compare.
    """
    declared = _item_datetime(item)
    if declared is None:
        return True
    if isinstance(value, str):
        value = _parse_timestamp(value)
    if not isinstance(value, datetime):
        return False
    return abs(_as_utc(value) - declared) <= _MIRROR_TIME_TOL


def _bbox_agrees(item: Node, value: Any) -> bool:
    """Does one mirror row's covering box reproduce the item's ``bbox``?"""
    declared = as_bbox(item.data.get("bbox"))
    if declared is None:
        return True  # no declared bbox is nothing to compare against
    if not isinstance(value, list) or len(value) != len(declared):
        return False
    return all(abs(a - b) <= _MIRROR_COORD_TOL for a, b in zip(declared, value, strict=True))


def _item_datetime(item: Node) -> datetime | None:
    properties = item.data.get("properties")
    raw = properties.get("datetime") if isinstance(properties, dict) else None
    return _parse_timestamp(raw) if isinstance(raw, str) else None


def _parse_timestamp(value: str) -> datetime | None:
    """An RFC 3339 timestamp as an aware UTC datetime, or None if unparseable."""
    match = _RFC3339.match(value.strip())
    if match is None:
        return None
    fraction = (match["fraction"] or "").ljust(6, "0")[:6]
    offset = (match["offset"] or "Z").replace("Z", "+00:00").replace("z", "+00:00")
    if len(offset) == 5:  # +HHMM, which fromisoformat wants as +HH:MM
        offset = f"{offset[:3]}:{offset[3:]}"
    try:
        return _as_utc(datetime.fromisoformat(f"{match['stamp']}.{fraction}{offset}"))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    """A naive timestamp is read as UTC: stac-geoparquet writes the column so."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sample(ids: list[str], limit: int = 3) -> str:
    shown = ", ".join(ids[:limit])
    return shown if len(ids) <= limit else f"{shown}, …"
