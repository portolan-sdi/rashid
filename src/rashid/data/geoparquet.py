"""GeoParquet metadata, storage checks, and table declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa
from pyproj import CRS

from rashid import _json
from rashid.catalog import Node
from rashid.data import DataDefect
from rashid.data._files import AssetFiles
from rashid.data.spatial import Geo, as_bbox, bbox_union, unreadable_spatial_metadata
from rashid.model import Severity
from rashid.registry import (
    DAT_COLUMN_DESCRIPTIONS,
    DAT_GEOPARQUET_VERSION,
    DAT_ORDERING,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_TABULAR,
    DAT_VECTOR_COLUMNS,
)
from rashid.rules._common import (
    has_table_columns,
    roles_of,
    table_declarations,
)

# formats.md:75 — a GeoParquet row group MUST hold no more than this many rows.
_MAX_ROW_GROUP_ROWS = 150_000

# formats.md:38 — spatial ordering passes on either criterion.
_MAX_OVERLAP_FRACTION = 0.30  # < 30% of consecutive row-group pairs may overlap
# Row-group boxes average < 30% of the file extent. formats.md sets the figure from
# what a Hilbert sort achieves: boxes land near 27% of the extent at five row groups
# and fall away as groups are added. A flat 25% rejected well-sorted five- and
# six-group files for their row-group count rather than their ordering.
_MAX_LOCALITY_RATIO = 0.30

# formats.md:55 — both checks measure a percentage across the row groups, and with
# fewer than five groups that percentage cannot land on a useful value. Three groups
# can only produce an overlap of 0%, 50%, or 100%, and two or three boxes cannot
# average less than 30% of the extent however well the rows are sorted. PORTO-FMT-044
# forbids failing a file on a threshold its row-group count puts out of reach, so
# below five groups neither check runs.
_MIN_ORDERING_ROW_GROUPS = 5

# Row ordering is a separate rule that applies to every file, whatever its row-group
# layout (formats.md:30). It is checked by splitting the rows into the groups a
# conforming writer would have produced, which works at any row-group count and is
# the only check available below five. Ten chunks is comfortably above
# _MIN_ORDERING_ROW_GROUPS, so the 30% limit means something; below the row floor a
# chunk covers too few points to say anything. Neither number is a spec threshold.
_ORDERING_CHUNKS = 10
_MIN_CHUNK_ROWS = 20

# --- GeoParquet cloud-native structure -------------------------------------


def check_geoparquet(
    key: str, files: AssetFiles, *, subject: str | None = None
) -> list[DataDefect]:
    """A GeoParquet asset MUST have bounded row groups, per-row-group spatial
    statistics, and spatial ordering (formats.md:30,64,75).

    Parquet and GeoParquet share the ``application/vnd.apache.parquet`` media
    type. Native GEOMETRY/GEOGRAPHY columns are spatial even without the
    required ``geo`` key. Such files fail the GeoParquet declaration check and
    still run the row-group checks; other files without the key are tabular.

    ``subject`` names the file in the message. It defaults to the asset the key
    belongs to; the partition pass passes the matched path instead, since a
    partition file is addressed by the collection's glob and is nobody's asset.
    """
    subject = subject or f"asset '{key}'"
    try:
        parquet = files.parquet()
    except Exception:  # unreadable Parquet: format/checksum checks own it
        return []

    geo = geo_metadata(parquet)
    if isinstance(geo, _MalformedGeo):
        return [unreadable_spatial_metadata(key, geo.message, subject=subject)]
    defects: list[DataDefect] = []
    if geo is None and (primary := _native_geometry(parquet)) is not None:
        defects.append(
            DataDefect(
                DAT_GEOPARQUET_VERSION,
                Severity.ERROR,
                f"{subject} is not conformant GeoParquet 2.0: native geometry needs a 'geo' metadata key",
                key,
            )
        )
        geo = {"version": "2.0", "primary_column": primary}
    if not isinstance(geo, dict):
        return []  # plain Parquet
    defects.extend(_check_geoparquet_version(key, geo, subject=subject))
    meta = parquet.metadata
    row_counts = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
    if any(n > _MAX_ROW_GROUP_ROWS for n in row_counts):
        defects.append(
            DataDefect(
                DAT_ROWGROUP_SIZE,
                Severity.ERROR,
                f"{subject} has a row group of {max(row_counts)} rows, "
                f"over the {_MAX_ROW_GROUP_ROWS} limit",
                key,
            )
        )

    bboxes, stat_defects = _rowgroup_stat_defects(key, parquet, geo, subject=subject)
    defects.extend(stat_defects)
    if bboxes is None:
        return defects  # without per-row-group boxes, ordering cannot be judged

    ordering = _ordering_defect(key, parquet, geo, bboxes, subject=subject)
    if ordering is not None:
        defects.append(ordering)
    return defects


def _check_geoparquet_version(
    key: str, geo: dict[str, Any], *, subject: str | None = None
) -> list[DataDefect]:
    """The ``geo`` metadata MUST declare GeoParquet 1.1 or 2.x.

    formats.md:25: "Data MUST be provided in GeoParquet 1.1 or 2.0". A file
    with ``geo`` metadata but an older version (1.0, the common legacy case)
    lacks the covering-column machinery the rest of the format MUSTs assume.
    """
    subject = subject or f"asset '{key}'"
    version = geo.get("version")
    if isinstance(version, str) and (version == "1.1" or version.startswith(("1.1.", "2."))):
        return []
    described = repr(version) if version is not None else "no version"
    return [
        DataDefect(
            DAT_GEOPARQUET_VERSION,
            Severity.ERROR,
            f"{subject} geo metadata declares {described}; data must be GeoParquet 1.1 or 2.x",
            key,
        )
    ]


def _native_geometry(parquet: Any) -> str | None:
    """The first native GEOMETRY or GEOGRAPHY leaf, without guessing by name."""
    return next(
        (
            parquet.schema.column(i).path
            for i in range(len(parquet.schema))
            if parquet.schema.column(i).logical_type.type in {"GEOMETRY", "GEOGRAPHY"}
        ),
        None,
    )


@dataclass(frozen=True)
class _MalformedGeo:
    message: str


def geo_metadata(parquet: Any) -> dict[str, Any] | _MalformedGeo | None:
    """Distinguish an absent key, malformed JSON, and a parsed metadata object."""
    raw = (parquet.schema_arrow.metadata or {}).get(b"geo")
    if raw is None:
        return None
    try:
        geo = _json.loads(raw)
    except (ValueError, TypeError) as exc:
        return _MalformedGeo(str(exc))
    if not isinstance(geo, dict):
        return _MalformedGeo(f"'{type(geo).__name__}' object has no attribute 'get'")
    return geo


def bbox_struct(
    parquet: Any, geo: dict[str, Any], *, policy: Literal["rows", "statistics"] = "rows"
) -> tuple[str, list[str]] | None:
    """Resolve covering paths for either row structs or footer statistics.

    ``rows`` requires four leaves in one shallow struct. ``statistics`` accepts
    arbitrary leaf paths, returned as dotted names with an empty root.
    """
    columns = geo.get("columns")
    entry = columns.get(geo.get("primary_column")) if isinstance(columns, dict) else None
    covering = entry.get("covering") if isinstance(entry, dict) else None
    bbox = covering.get("bbox") if isinstance(covering, dict) else None
    if not isinstance(bbox, dict):
        return None
    paths: list[Any] = [bbox.get(corner) for corner in ("xmin", "ymin", "xmax", "ymax")]
    if policy == "statistics":
        try:
            names = [".".join(path) for path in paths]
        except TypeError:
            return None
        return "", names
    if not all(isinstance(path, list) and len(path) == 2 for path in paths):
        return None
    roots = {str(path[0]) for path in paths}
    if len(roots) == 1 and (root := roots.pop()) in parquet.schema_arrow.names:
        return root, [str(path[1]) for path in paths]
    return None


def bbox_column_values(table: Any, bbox: tuple[str, list[str]]) -> list[Any]:
    """Per-row [xmin, ymin, xmax, ymax], or None for a row that carries none."""
    root, fields = bbox
    values: list[Any] = []
    for row in table.column(root).to_pylist():
        corners = [row.get(field) for field in fields] if isinstance(row, dict) else []
        numeric = [float(corner) for corner in corners if isinstance(corner, (int, float))]
        values.append(numeric if len(numeric) == 4 else None)
    return values


# --- tabular collections ----------------------------------------------------


def check_tabular(
    node: Node, key: str, asset: dict[str, Any], files: AssetFiles
) -> list[DataDefect]:
    """The Tabular Data SHOULDs for a plain-Parquet collection-level data asset.

    formats.md, Tabular Data: "Tabular collections SHOULD populate
    ``extent.temporal`` when the data has a time dimension, and SHOULD document
    their columns with the STAC table extension (``table:columns`` with names,
    types, and descriptions)." A tabular collection is a Parquet data asset
    with no ``geo`` metadata key, exposed at collection level (the single-file
    collection pattern the same section mandates); non-data Parquet and
    item-level assets are out of scope. The time dimension is read from the
    file itself: a temporal (timestamp/date) column is the machine-detectable
    signal, so its absence keeps the extent SHOULD silent rather than guessed.
    """
    if node.kind != "collection" or "data" not in roles_of(asset):
        return []
    try:
        parquet = files.parquet()
    except Exception:  # unreadable Parquet: format/checksum checks own it
        return []
    if geo_metadata(parquet) is not None or _native_geometry(parquet) is not None:
        return []  # GeoParquet: the geospatial storage rules own it
    defects: list[DataDefect] = []
    if not has_table_columns(node, asset):
        defects.append(
            DataDefect(
                DAT_TABULAR,
                Severity.WARNING,
                f"tabular asset '{key}': the collection does not document its columns "
                "with the table extension (table:columns)",
                key,
            )
        )
    if _has_temporal_column(parquet) and not _has_temporal_extent(node):
        defects.append(
            DataDefect(
                DAT_TABULAR,
                Severity.WARNING,
                f"tabular asset '{key}' carries a temporal column but the collection "
                "does not populate extent.temporal",
                key,
            )
        )
    return defects


def check_vector_columns(
    node: Node, key: str, asset: dict[str, Any], files: AssetFiles
) -> list[DataDefect]:
    """The table-extension SHOULDs for a GeoParquet data asset.

    formats.md, Vector Data: a vector collection SHOULD carry ``table:columns``
    on the collection itself (PORTO-FMT-046), and an item that carries a
    GeoParquet data asset SHOULD carry the same field in its ``properties``
    (PORTO-FMT-047). Without it a client discovers a hundred attribute columns
    only by reading the Parquet footer. Plain Parquet is the tabular SHOULD's
    business, and a partitioned collection has no data asset to reach here, so
    its columns are checked wherever its partition files are described.
    """
    if "data" not in roles_of(asset):
        return []
    try:
        parquet = files.parquet()
    except Exception:  # unreadable Parquet: format/checksum checks own it
        return []
    geo = geo_metadata(parquet)
    if isinstance(geo, _MalformedGeo) or (geo is None and _native_geometry(parquet) is None):
        return []  # plain Parquet: the Tabular Data SHOULDs own it
    if has_table_columns(node, asset):
        return []
    where = (
        "the item does not document its columns in properties"
        if node.kind == "item"
        else "the collection does not document its columns"
    )
    return [
        DataDefect(
            DAT_VECTOR_COLUMNS,
            Severity.WARNING,
            f"vector asset '{key}': {where} with the table extension (table:columns)",
            key,
        )
    ]


def check_column_descriptions(node: Node, key: str, asset: dict[str, Any]) -> list[DataDefect]:
    """Every ``table:columns`` entry of a data asset carries a description.

    formats.md, Vector Data: "Every entry in ``table:columns`` SHOULD include a
    ``description`` when a source defines the column" (PORTO-FMT-077). A name
    and a type are already in the Parquet footer. When no source defines a
    column, the publisher leaves the description out and names the column in
    the collection README (PORTO-FMT-078). The validator cannot tell these
    columns from undocumented ones, so it reports a warning for both. It checks
    presence only. It reads no file.
    """
    if "data" not in roles_of(asset):
        return []
    for holder in table_declarations(node, asset):
        columns = holder.get("table:columns")
        if isinstance(columns, list) and columns:
            break
    else:
        return []  # no declaration: PTL-DAT-015 and PTL-DAT-017 own it
    bare = [
        str(column.get("name", "?"))
        for column in columns
        if isinstance(column, dict) and not str(column.get("description") or "").strip()
    ]
    if not bare:
        return []
    shown = ", ".join(bare[:5])
    more = f", and {len(bare) - 5} more" if len(bare) > 5 else ""
    return [
        DataDefect(
            DAT_COLUMN_DESCRIPTIONS,
            Severity.WARNING,
            f"asset '{key}': {len(bare)} of {len(columns)} table:columns entries carry "
            f"no description ({shown}{more}). If no source defines a column, name it "
            "in the collection README instead.",
            key,
        )
    ]


def _has_temporal_column(parquet: Any) -> bool:
    return any(pa.types.is_temporal(field.type) for field in parquet.schema_arrow)


def _has_temporal_extent(node: Node) -> bool:
    extent = node.data.get("extent")
    temporal = extent.get("temporal") if isinstance(extent, dict) else None
    intervals = temporal.get("interval") if isinstance(temporal, dict) else None
    if not isinstance(intervals, list):
        return False
    return any(
        isinstance(interval, list) and any(bound is not None for bound in interval)
        for interval in intervals
    )


def _rowgroup_stat_defects(
    key: str, parquet: Any, geo: dict[str, Any], *, subject: str | None = None
) -> tuple[list[tuple[float, float, float, float]] | None, list[DataDefect]]:
    """Per-row-group [minx, miny, maxx, maxy] boxes, plus statistics defects.

    formats.md:64 accepts two satisfiers: a 1.1 ``bbox`` covering column whose
    leaf fields carry Parquet min/max, or — for GeoParquet 2.x / Parquet
    ``GEOMETRY`` — native ``GeospatialStatistics`` per row group. Neither
    source yielding a box for every row group is the MUST failure (ERROR).
    Native statistics alone still satisfy the MUST, but formats.md keeps the
    covering column "RECOMMENDED even where native statistics exist, since it
    adds page-level min/max stats that enable finer-grained pruning" — that
    SHOULD surfaces as a WARNING.
    """
    subject = subject or f"asset '{key}'"
    boxes = _covering_bboxes(parquet, geo)
    if boxes is not None:
        return boxes, []
    boxes = _native_bboxes(parquet, geo)
    if boxes is None:
        return None, [
            DataDefect(
                DAT_ROWGROUP_STATS,
                Severity.ERROR,
                f"{subject} provides no per-row-group spatial statistics "
                "(no bbox covering column with min/max stats, nor native GeospatialStatistics)",
                key,
            )
        ]
    return boxes, [
        DataDefect(
            DAT_ROWGROUP_STATS,
            Severity.WARNING,
            f"{subject} relies on native GeospatialStatistics without a bbox "
            "covering column; a covering column remains recommended for "
            "page-level pruning",
            key,
        )
    ]


def _native_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float]] | None:
    """Per-row-group boxes from Parquet native ``GeospatialStatistics``.

    Read from the primary geometry column's chunk metadata (pyarrow >= 21
    exposes ``geo_statistics``; older versions have no attribute and fall
    through to None, keeping the covering column as the only satisfier).
    """
    primary = geo.get("primary_column")
    if not isinstance(primary, str):
        return None
    meta = parquet.metadata
    index = _column_index(meta)
    j = index.get(primary)
    if j is None:
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for i in range(meta.num_row_groups):
        stats = getattr(meta.row_group(i).column(j), "geo_statistics", None)
        if stats is None:
            return None
        corners = (stats.xmin, stats.ymin, stats.xmax, stats.ymax)
        if None in corners:
            return None
        boxes.append(tuple(float(c) for c in corners))  # type: ignore[arg-type]
    return boxes


def _covering_leaf_indices(parquet: Any, geo: dict[str, Any]) -> dict[str, int] | None:
    """Resolve all four covering leaves only when they exist in the Parquet schema."""
    covering = bbox_struct(parquet, geo, policy="statistics")
    if covering is None:
        return None
    index = _column_index(parquet.metadata)
    paths = covering[1]
    if not all(path in index for path in paths):
        return None
    return dict(zip(("xmin", "ymin", "xmax", "ymax"), (index[path] for path in paths), strict=True))


def _covering_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float]] | None:
    """Per-row-group [minx, miny, maxx, maxy] from the bbox covering column's stats.

    Returns None when the file has no 1.1 ``bbox`` covering column whose leaf
    fields carry Parquet min/max statistics.
    """
    leaves = _covering_leaf_indices(parquet, geo)
    if leaves is None:
        return None
    meta = parquet.metadata

    boxes: list[tuple[float, float, float, float]] = []
    for i in range(meta.num_row_groups):
        group = meta.row_group(i)
        stats = [group.column(index).statistics for index in leaves.values()]
        if all(
            stat is not None and stat.has_null_count and stat.null_count == group.num_rows
            for stat in stats
        ):
            continue  # a known geometry-less group is prunable without a box
        try:
            minx = group.column(leaves["xmin"]).statistics.min
            miny = group.column(leaves["ymin"]).statistics.min
            maxx = group.column(leaves["xmax"]).statistics.max
            maxy = group.column(leaves["ymax"]).statistics.max
        except AttributeError:
            return None  # a leaf without statistics does not qualify
        if None in (minx, miny, maxx, maxy):
            return None
        boxes.append((float(minx), float(miny), float(maxx), float(maxy)))
    return boxes or None


def _column_index(meta: Any) -> dict[str, int]:
    if meta.num_row_groups == 0:
        return {}
    group = meta.row_group(0)
    return {group.column(j).path_in_schema: j for j in range(group.num_columns)}


def _ordering_defect(
    key: str,
    parquet: Any,
    geo: dict[str, Any],
    bboxes: list[tuple[float, float, float, float]],
    *,
    subject: str,
) -> DataDefect | None:
    """One verdict from footer criteria, a footer proof, or row chunks."""
    metrics_apply = len(bboxes) >= _MIN_ORDERING_ROW_GROUPS
    if metrics_apply and not _is_spatially_ordered(bboxes):
        return DataDefect(
            DAT_ORDERING,
            Severity.ERROR,
            f"{subject} rows are not spatially ordered: row groups overlap heavily "
            "and lack locality, so a reader cannot skip them",
            key,
        )
    rows = parquet.metadata.num_rows
    groups = parquet.metadata.num_row_groups
    row_counts = [parquet.metadata.row_group(i).num_rows for i in range(groups)]
    if rows < _ORDERING_CHUNKS * _MIN_CHUNK_ROWS or (
        metrics_apply and _footer_proves_row_ordering(parquet, geo, bboxes, row_counts)
    ):
        return None
    row_boxes = _row_bboxes(parquet, geo)
    # Re-apply the floor to the boxes that survived, not to the row count. Rows
    # without geometry carry no covering box, so a file can hold enough rows and
    # still leave too few boxes for a chunk's box to describe anything. Below the
    # floor the chunks hold at most one box each, which measures nothing: one box
    # leaves ``_is_spatially_ordered`` no consecutive pair to divide by, and a
    # handful yields a verdict drawn from a sample far under the floor.
    if row_boxes is None or len(row_boxes) < _ORDERING_CHUNKS * _MIN_CHUNK_ROWS:
        if metrics_apply:
            return None
        if row_boxes is None:
            why = "with no bbox covering column to read"
        else:
            measured = len(row_boxes)
            verb = "carries" if measured == 1 else "carry"
            why = f"of which {measured} {verb} a covering box"
        return DataDefect(
            DAT_ORDERING,
            Severity.INFO,
            f"{subject} holds {rows} rows in {_plural(groups, 'row group')} "
            f"{why}, so spatial ordering could not be evaluated",
            key,
        )
    if _is_spatially_ordered(_chunked_bboxes(row_boxes)):
        return None
    return DataDefect(
        DAT_ORDERING,
        Severity.ERROR,
        f"{subject} rows are not spatially ordered: {rows} rows in "
        f"{_plural(groups, 'row group')} do not cluster spatially, so a reader "
        "cannot skip any part of the file",
        key,
    )


def _footer_proves_row_ordering(
    parquet: Any,
    geo: dict[str, Any],
    bboxes: list[tuple[float, float, float, float]],
    row_counts: list[int],
) -> bool:
    """Can row-group bounds prove that the synthetic row chunks pass?

    Each synthetic chunk gets the union of every row group it intersects. That
    box can be larger than the chunk's exact box, but never smaller. Passing
    with these conservative boxes therefore proves that the exact boxes pass.

    This shortcut requires a complete covering column. Native statistics do
    not expose per-row null counts, and null rows change the synthetic chunk
    boundaries after :func:`_row_bboxes` removes them.
    """
    covering_boxes = _covering_bboxes(parquet, geo)
    if covering_boxes != bboxes or not _covering_has_no_nulls(parquet, geo):
        return False
    chunk_boxes = _conservative_chunk_bboxes(bboxes, row_counts)
    return len(chunk_boxes) >= _MIN_ORDERING_ROW_GROUPS and _is_spatially_ordered(chunk_boxes)


def _covering_has_no_nulls(parquet: Any, geo: dict[str, Any]) -> bool:
    """Do all four covering leaves have known zero null counts?"""
    leaves = _covering_leaf_indices(parquet, geo)
    if leaves is None:
        return False
    meta = parquet.metadata
    for i in range(meta.num_row_groups):
        group = meta.row_group(i)
        try:
            if any(group.column(index).statistics.null_count != 0 for index in leaves.values()):
                return False
        except AttributeError:
            return False
    return True


def _conservative_chunk_bboxes(
    bboxes: list[tuple[float, float, float, float]], row_counts: list[int]
) -> list[tuple[float, float, float, float]]:
    """Bound each synthetic row chunk with the row groups it intersects."""
    rows = sum(row_counts)
    if rows == 0:
        return []
    size = -(-rows // _ORDERING_CHUNKS)
    group_ranges: list[tuple[int, int, tuple[float, float, float, float]]] = []
    group_start = 0
    for count, bbox in zip(row_counts, bboxes, strict=True):
        group_ranges.append((group_start, group_start + count, bbox))
        group_start += count
    chunks = []
    for start in range(0, rows, size):
        end = min(start + size, rows)
        intersecting = [bbox for left, right, bbox in group_ranges if left < end and right > start]
        if intersecting:
            chunks.append(bbox_union(intersecting))
    return chunks


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _chunked_bboxes(
    row_boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Box each contiguous chunk of rows, as row groups would have been.

    Partitioning reads FMT-006's row-group criteria as a measurement method
    rather than as the requirement, which the spec implies but does not say.
    portolan-spec#100 records the ambiguity: both tests compare row groups, so
    a file with one group has no stated evaluation.
    """
    size = -(-len(row_boxes) // _ORDERING_CHUNKS)
    return [bbox_union(row_boxes[i : i + size]) for i in range(0, len(row_boxes), size)]


def _row_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float]] | None:
    """Per-row [minx, miny, maxx, maxy] from the bbox covering column's values.

    Reads the four covering leaves and not the geometry, so the cost is four
    float64 columns. Returns None when the file has no 1.1 covering column, or
    when its leaves sit deeper than the single struct level the spec uses.

    Rows whose covering values are null are skipped rather than abandoning the
    file. GeoParquet permits a null geometry, writers give those rows a null
    covering box, and a row with no geometry has no position — so it cannot be
    out of spatial order, and one of them must not cost the whole file its
    ordering check. Returns None only when no row has a box at all.
    """
    covering = bbox_struct(parquet, geo)
    if covering is None:
        return None
    root, fields = covering
    try:
        flat = parquet.read(columns=[root]).flatten()
        corners = [flat.column(f"{root}.{field}").to_pylist() for field in fields]
    except Exception:  # unreadable column: the checksum check owns bad bytes
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for corner_values in zip(*corners, strict=True):
        if any(value is None for value in corner_values):
            continue  # geometry-less row: no position, so nothing to order
        boxes.append(
            (
                float(corner_values[0]),
                float(corner_values[1]),
                float(corner_values[2]),
                float(corner_values[3]),
            )
        )
    return boxes or None


def _is_spatially_ordered(bboxes: list[tuple[float, float, float, float]]) -> bool:
    """True if row groups are spatially ordered by either spec criterion (formats.md:38).

    Callers must hold the applicability guard themselves. Below
    ``_MIN_ORDERING_ROW_GROUPS`` the two criteria say nothing, so a file that
    thin is either left alone or measured over its rows instead; see
    :func:`check_geoparquet` and :func:`_ordering_defect`.
    """
    pairs = len(bboxes) - 1
    overlaps = sum(_bbox_overlaps(bboxes[i], bboxes[i + 1]) for i in range(pairs))
    if overlaps / pairs < _MAX_OVERLAP_FRACTION:
        return True  # low overlap

    extent = bbox_union(bboxes)
    extent_area = _bbox_area(extent)
    if extent_area == 0:
        return True  # a single location — nothing to order
    mean_ratio = sum(_bbox_area(b) for b in bboxes) / len(bboxes) / extent_area
    return mean_ratio < _MAX_LOCALITY_RATIO


def _bbox_area(b: tuple[float, float, float, float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _bbox_overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def geo_from_parquet(files: AssetFiles) -> Geo | None:
    parquet = files.parquet()
    geo = geo_metadata(parquet)
    if geo is None and _native_geometry(parquet) is None:
        return None
    if geo is None or isinstance(geo, _MalformedGeo):
        # The GeoParquet checks report missing or malformed spatial metadata.
        return Geo(bbox=None, epsg=None, crs=None)
    primary = geo.get("primary_column")
    column = geo.get("columns", {}).get(primary, {})
    bbox = column.get("bbox")
    crs_obj = column.get("crs")
    crs = CRS.from_user_input(crs_obj) if crs_obj is not None else CRS.from_epsg(4326)
    return Geo(bbox=as_bbox(bbox), epsg=crs.to_epsg(), crs=crs)
