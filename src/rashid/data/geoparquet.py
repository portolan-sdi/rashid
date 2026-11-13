"""GeoParquet metadata, storage checks, and table declarations."""

from __future__ import annotations

import math
import re
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
    DAT_GEOPARQUET_VERSION,
    DAT_NATIVE_CRS,
    DAT_ORDERING,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_TABULAR,
    DAT_VECTOR_COLUMNS,
)
from rashid.rules._common import (
    has_table_columns,
    roles_of,
)

# formats.md:75 — a GeoParquet row group MUST hold no more than this many rows.
_MAX_ROW_GROUP_ROWS = 150_000


# formats.md:75 — a GeoParquet row group MUST hold no more than this many rows.
_MAX_ROW_GROUP_ROWS = 150_000

# formats.md:38 — the row-group check computes the expected share of the file's
# bytes a query window lets a reader skip, and compares that against an ideal
# tiling of the extent into the same number of row groups. The window's centre
# falls in a box with probability proportional to its rows and the window spans
# _QUERY_FRACTION of each dimension of that box, so empty extent cannot decide
# the verdict (spec PORTO-FMT-050/053; the outlier vectors pin it). It is an
# expectation, not a sample: the closed form is O(n^2), reads the footer only
# and has no seed, so another implementation reaches the same number.
_QUERY_FRACTION = 0.10
# The windows a verdict's message reports the read share for (PORTO-FMT-060
# names the absolute cost at the query window; PORTO-FMT-061 adds the 1% and
# 25% windows beside it).
_REPORT_FRACTIONS = (0.01, _QUERY_FRACTION, 0.25)
# 0.65 comes from the 2026-11 registry survey and its Hilbert-sort control
# (the spec's calibration directory): the 32 sorted copies eligible for a
# footer pass scored 0.696 to 0.944, so a 0.70 bar rejected an already-sorted
# file (Manitoba buildings, 0.696 before and after a re-sort) while 0.65
# accepts every tested sorted copy and still fails the shuffled band.
_MIN_SKIP_EFFICIENCY = 0.65
# A reference skip rate at or under this is zero: one row group, a window as
# large as the extent, or an extent with no area. Float noise, not a layout.
_NO_SKIP = 1e-9

# formats.md, the two paths: a file with eight or more row groups takes the
# footer check on its row-group boxes (PORTO-FMT-050). Below eight the grid
# reference is a poor model of a curve sort, so well-sorted files from several
# catalogs score 0.60 to 0.88 at five row groups and 0.76 to 0.94 at eight
# (rashid#174 review, spec point 3). PORTO-FMT-044 forbids judging such a file
# by its row-group boxes, so it takes the row check instead.
_MIN_ORDERING_ROW_GROUPS = 8

# Row ordering is a separate rule that applies to every file, whatever its row-group
# layout (formats.md:30). It is checked by splitting the rows into the groups a
# conforming writer would have produced and applying the same skip-rate metric
# to those chunks against a reference of the same count, which works at any
# row-group count and judges every file below eight row groups. Ten chunks
# keeps the reference above the row-group floor; under _MIN_CHUNK_ROWS rows a chunk's box
# describes too few points to say anything, so a file needs _MIN_ORDERING_ROWS
# rows before the row check judges it. Neither number is a spec threshold.
_ORDERING_CHUNKS = 10
_MIN_CHUNK_ROWS = 20
_MIN_ORDERING_ROWS = _ORDERING_CHUNKS * _MIN_CHUNK_ROWS

# --- GeoParquet cloud-native structure -------------------------------------


def check_geoparquet(
    key: str, files: AssetFiles, *, subject: str | None = None
) -> list[DataDefect]:
    """A GeoParquet asset MUST have bounded row groups, per-row-group spatial
    statistics, and spatial ordering (formats.md:30,64,75).

    Parquet and GeoParquet share the ``application/vnd.apache.parquet`` media
    type. A file is geospatial when it carries a ``geo`` metadata key or a
    column of the native Parquet ``GEOMETRY``/``GEOGRAPHY`` logical type
    (PORTO-FMT-055); only a file with neither is plain tabular Parquet. A
    native-geo file with no ``geo`` key is the spec's third accepted form, so
    the missing key is not faulted — its CRS rules are (PORTO-FMT-056/057) —
    and Iceberg writers produce exactly that file (#200).

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
    native = _native_geo_columns(parquet)
    if geo is None and native:
        # PORTO-FMT-055: the spec's third accepted form. The missing key is
        # not faulted; the native crs field is held to PORTO-FMT-056/057
        # instead, and the version rule applies only to a ``geo`` key.
        defects.extend(_check_native_crs(key, parquet, native, subject=subject))
        geo = {"primary_column": _primary_geometry_column(parquet, {})}
    elif isinstance(geo, dict):
        defects.extend(_check_geoparquet_version(key, geo, subject=subject))
    if not isinstance(geo, dict):
        return []  # plain Parquet
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
    shape = (sum(row_counts), len(row_counts))
    if bboxes is None:
        # Without per-row-group boxes the footer check cannot run, so the
        # row check judges the file (formats.md, Row check).
        defects.extend(_row_ordering_defects(key, parquet, geo, shape, subject=subject))
        return defects

    layout = _RowGroupLayout(bboxes, row_counts, _rowgroup_compressed_sizes(meta))
    defects.extend(_ordering_defects(key, parquet, geo, layout, subject=subject))
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
    """The primary native GEOMETRY/GEOGRAPHY column, None when the file has none."""
    return _primary_geometry_column(parquet, {})


def _native_geo_columns(parquet: Any) -> list[str]:
    """Columns whose Parquet logical type is ``GEOMETRY`` or ``GEOGRAPHY``.

    Read from the Parquet schema, not the Arrow one, so detection needs no
    registered geoarrow extension. pyarrow >= 21 names the type through
    ``logical_type``; an older pyarrow reports it as unknown and the file
    falls back to the ``geo``-key path.
    """
    schema = getattr(parquet.metadata, "schema", None)
    if schema is None:
        return []
    names: list[str] = []
    for i in range(len(schema)):
        column = schema.column(i)
        logical = getattr(column, "logical_type", None)
        if logical is None:
            continue
        try:
            kind = _json.loads(logical.to_json()).get("Type")
        except (ValueError, TypeError, AttributeError):
            continue
        if kind in ("Geometry", "Geography"):
            names.append(column.name)
    return names


def _primary_geometry_column(parquet: Any, geo: dict[str, Any]) -> str | None:
    """The column the per-row-group statistics are read from.

    The ``geo`` key names it; a native-geo file without one defaults to the
    column named ``geometry``, else the first native geospatial column.
    """
    primary = geo.get("primary_column")
    if isinstance(primary, str):
        return primary
    native = _native_geo_columns(parquet)
    if not native:
        return None
    return "geometry" if "geometry" in native else native[0]


def _native_crs(parquet: Any, name: str) -> str | None:
    """The ``crs`` string of a native geospatial column, None when absent."""
    schema = getattr(parquet.metadata, "schema", None)
    if schema is None:
        return None
    for i in range(len(schema)):
        column = schema.column(i)
        if column.name != name:
            continue
        logical = getattr(column, "logical_type", None)
        if logical is None:
            return None
        try:
            crs = _json.loads(logical.to_json()).get("crs")
        except (ValueError, TypeError, AttributeError):
            return None
        return crs if isinstance(crs, str) else None
    return None


# A CRS registry identifier: `<authority>:<code>`, which also matches
# `srid:<identifier>`. Resolving one needs a registry, which is why PORTO-FMT-057
# prefers inline PROJJSON and keeps this form a warning rather than an error.
_AUTHORITY_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.]+\Z")


def _check_native_crs(
    key: str, parquet: Any, columns: list[str], *, subject: str | None = None
) -> list[DataDefect]:
    """The CRS of each native geospatial column resolves from the file alone.

    formats.md: the native ``crs`` value MUST use one of the four formats the
    Parquet geospatial specification suggests, and an absent value is
    OGC:CRS84 (PORTO-FMT-056).
    Inline PROJJSON and a ``projjson:<key>`` reference into the file metadata
    resolve from the file alone; an ``<authority>:<code>`` or ``srid:<identifier>``
    value needs a CRS registry, so it is a warning (PORTO-FMT-057). Anything
    else is an error. These rules only reach a file with no ``geo`` key —
    with one, the key's own ``crs`` field governs and the consistency check
    reads that.
    """
    subject = subject or f"asset '{key}'"
    defects: list[DataDefect] = []
    for name in columns:
        crs = _native_crs(parquet, name)
        if crs is None:
            continue  # absent: OGC:CRS84 by definition
        value = crs.strip()
        if not value or value == "OGC:CRS84":
            continue
        if value.startswith("{"):
            try:
                _json.loads(value)
                continue  # inline PROJJSON
            except ValueError:
                defects.append(
                    DataDefect(
                        DAT_NATIVE_CRS,
                        Severity.ERROR,
                        f"{subject} geometry column '{name}' carries a crs that opens "
                        "like JSON but does not parse; it must use one of the four "
                        "formats the Parquet geospatial specification suggests",
                        key,
                    )
                )
                continue
        if value.startswith("projjson:"):
            defects.extend(_projjson_reference_defects(key, parquet, name, value, subject=subject))
            continue
        if _AUTHORITY_CODE.match(value):
            defects.append(
                DataDefect(
                    DAT_NATIVE_CRS,
                    Severity.WARNING,
                    f"{subject} geometry column '{name}' declares its CRS as '{value}', "
                    "which needs a CRS registry to resolve; inline PROJJSON lets a "
                    "reader resolve it from the file alone",
                    key,
                )
            )
            continue
        defects.append(
            DataDefect(
                DAT_NATIVE_CRS,
                Severity.ERROR,
                f"{subject} geometry column '{name}' crs '{value}' is not one of the four "
                "formats the Parquet geospatial specification suggests",
                key,
            )
        )
    return defects


def _projjson_reference_defects(
    key: str, parquet: Any, name: str, value: str, *, subject: str
) -> list[DataDefect]:
    """A ``projjson:<key>`` crs resolves against the file's key-value metadata."""
    meta_key = value[len("projjson:") :]
    kv = getattr(parquet.metadata, "metadata", None) or {}
    raw = kv.get(meta_key.encode())
    if raw is None:
        return [
            DataDefect(
                DAT_NATIVE_CRS,
                Severity.ERROR,
                f"{subject} geometry column '{name}' crs names file metadata key "
                f"'{meta_key}', which the file does not carry",
                key,
            )
        ]
    try:
        _json.loads(raw)
    except (ValueError, TypeError):
        return [
            DataDefect(
                DAT_NATIVE_CRS,
                Severity.ERROR,
                f"{subject} geometry column '{name}' crs names file metadata key "
                f"'{meta_key}', which does not hold PROJJSON",
                key,
            )
        ]
    return []


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


def _rowgroup_compressed_sizes(meta: Any) -> list[float]:
    """Each row group's compressed bytes, the footer check's byte weights.

    The sum over each group's column chunks, so the number is footer-only and
    a remote validator reads the same value as a local one.
    """
    sizes: list[float] = []
    for i in range(meta.num_row_groups):
        group = meta.row_group(i)
        sizes.append(
            float(sum(group.column(j).total_compressed_size for j in range(group.num_columns)))
        )
    return sizes


@dataclass(frozen=True)
class _RowGroupLayout:
    """The footer facts the ordering verdict reads: boxes, rows, bytes."""

    bboxes: list[tuple[float, float, float, float]]
    row_counts: list[int]
    sizes: list[float]


def _ordering_defects(
    key: str,
    parquet: Any,
    geo: dict[str, Any],
    layout: _RowGroupLayout,
    *,
    subject: str,
) -> list[DataDefect]:
    """Spatial ordering, judged on one of the spec's two paths
    (formats.md:30,38; PORTO-FMT-050, PORTO-FMT-044).

    At eight or more row groups the footer check decides from the row-group
    boxes and no row is read. Where those boxes give an undefined efficiency,
    as with a point extent, the row check decides instead. Below eight row
    groups the boxes give no verdict: their statistics are reported at INFO,
    which PORTO-FMT-044 allows, and the row check judges the rows in
    file-order chunks by the same metric and bar. One file, one verdict: the
    footer verdict and the row check never both run, so they cannot disagree.
    """
    groups = len(layout.bboxes)
    rows = sum(layout.row_counts)
    weights = [float(n) for n in layout.row_counts]
    score = _pruning_score(layout.bboxes, weights, layout.sizes)
    verdict = _footer_verdict(score)
    if verdict == "pass":
        return []
    if verdict == "fail":
        summary = _pruning_summary(score, "row group", judged=True)
        return [
            DataDefect(
                DAT_ORDERING,
                Severity.ERROR,
                f"{subject} rows are not spatially ordered across its "
                f"{_plural(groups, 'row group')}: {summary}",
                key,
            )
        ]
    defects: list[DataDefect] = []
    if 1 < groups < _MIN_ORDERING_ROW_GROUPS and rows >= _MIN_ORDERING_ROWS:
        # One box is the extent itself and has no layout to describe; under the
        # row floor the row check's own INFO says why there is no verdict.
        summary = _pruning_summary(score, "row group", judged=False)
        defects.append(
            DataDefect(
                DAT_ORDERING,
                Severity.INFO,
                f"{subject} has {_plural(groups, 'row group')}, under the "
                f"{_MIN_ORDERING_ROW_GROUPS} the footer check needs, so its rows are "
                f"judged instead and its layout is only reported: {summary}",
                key,
            )
        )
    defects.extend(_row_ordering_defects(key, parquet, geo, (rows, groups), subject=subject))
    return defects


def _row_ordering_defects(
    key: str,
    parquet: Any,
    geo: dict[str, Any],
    shape: tuple[int, int],
    *,
    subject: str | None = None,
) -> list[DataDefect]:
    """The row check, PORTO-FMT-006's test: the path for a file with fewer
    than eight row groups, or without per-row-group spatial statistics.

    formats.md:30 requires spatially ordered rows in every file, and the row
    check judges them by splitting them into the ten chunks a conforming
    writer would have produced. The reference stac-geoparquet
    writer shows why that matters. With no schema passed,
    ``parse_stac_items_to_arrow`` returns one contiguous record batch at any
    item count and ``to_parquet`` writes one row group per batch, leaving the
    row-group check a single box, which passes on any row order.

    The chunks are scored by the same metric as the row groups, against a
    reference of the same count (spec point 5). The retired flat 30%-of-extent
    limit assumed unsorted rows reach the whole extent, which a sparse extent
    defeats, and it disagreed with the row-group check about real files.

    Every reason the check cannot run is an INFO rather than silence
    (PORTO-FMT-052): a validator's silence reads as a pass. Under 200 rows
    the file has too few rows to judge, and an undefined efficiency, as with
    rows all at one point, gives no verdict either.
    """
    rows, groups = shape
    subject = subject or f"asset '{key}'"
    if rows < _MIN_ORDERING_ROWS:
        return [_unevaluated(key, subject, rows, groups, _floors_not_reached())]
    row_boxes = _row_bboxes(parquet, geo)
    # Re-apply the floor to the rows that carry a box, not to the row count.
    # Rows without geometry have no position; a file can hold enough rows and
    # still leave too few boxes for a chunk's box to describe anything.
    if row_boxes is None:
        why = " with no bbox covering column to read"
        return [_unevaluated(key, subject, rows, groups, why)]
    boxed = sum(1 for b in row_boxes if b is not None)
    chunks, weights = _chunked_bboxes(row_boxes)
    score = _pruning_score(chunks, weights, weights)
    verdict = _row_verdict(score, boxed)
    if verdict == "too-few-rows":
        verb = "carries" if boxed == 1 else "carry"
        why = f" of which {boxed} {verb} a covering box{_floors_not_reached()}"
        return [_unevaluated(key, subject, rows, groups, why)]
    if verdict == "none":
        return [_unevaluated(key, subject, rows, groups, _undefined_efficiency())]
    if verdict == "pass":
        return []
    # n is the number of chunks that carry a box (PORTO-FMT-006's test), so the
    # reference shrinks with them rather than staying at ten cells.
    split = f"split into {_ORDERING_CHUNKS} chunks in file order"
    if len(chunks) < _ORDERING_CHUNKS:
        split += f", {len(chunks)} of which carry a box"
    summary = _pruning_summary(score, "chunk", judged=True)
    return [
        DataDefect(
            DAT_ORDERING,
            Severity.ERROR,
            f"{subject} rows are not spatially ordered: {rows} rows in "
            f"{_plural(groups, 'row group')}, {split}, {summary}",
            key,
        )
    ]


def _footer_verdict(score: _PruningScore) -> str:
    """The footer check's verdict on row-group boxes, as PORTO-FMT-054's
    vectors name it: ``pass`` or ``fail`` from eight boxes with a defined
    efficiency (PORTO-FMT-050), otherwise ``row-check`` (PORTO-FMT-044)."""
    if score.count < _MIN_ORDERING_ROW_GROUPS or score.below_bar is None:
        return "row-check"
    return "fail" if score.below_bar else "pass"


def _row_verdict(score: _PruningScore, boxed_rows: int) -> str:
    """The row check's verdict on file-order chunks, as PORTO-FMT-054's
    vectors name it: ``too-few-rows`` under the 200-row floor
    (PORTO-FMT-052), ``none`` for an undefined efficiency, otherwise ``pass``
    or ``fail``. The floor counts the rows that carry a box."""
    if boxed_rows < _MIN_ORDERING_ROWS:
        return "too-few-rows"
    if score.below_bar is None:
        return "none"
    return "fail" if score.below_bar else "pass"


def _floors_not_reached() -> str:
    return (
        f", too few rows to judge: the row check needs {_MIN_ORDERING_ROWS} rows "
        "with a bbox covering column"
    )


def _undefined_efficiency() -> str:
    return ", whose chunk boxes leave the pruning efficiency undefined"


def _unevaluated(key: str, subject: str, rows: int, groups: int, why: str) -> DataDefect:
    """The row check could not run, and says why rather than staying silent."""
    return DataDefect(
        DAT_ORDERING,
        Severity.INFO,
        f"{subject} holds {rows} rows in {_plural(groups, 'row group')}{why}, so spatial "
        "ordering could not be evaluated",
        key,
    )


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _chunked_bboxes(
    row_boxes: list[tuple[float, float, float, float] | None],
) -> tuple[list[tuple[float, float, float, float]], list[float]]:
    """Box each of the ten file-order chunks, as row groups would have been.

    Chunk ``k`` holds the rows with index ``floor(k * N / 10) <= i <
    floor((k + 1) * N / 10)``, the boundaries PORTO-FMT-006's test pins, so
    every implementation splits an ``N`` not divisible by ten the same way
    (``numpy.array_split`` does not). A row without a box stays in its chunk
    and does not extend the box. A chunk with no boxed row at all contributes
    no box and is left out: ``n`` is then the number of chunks that carry a
    box, and the reference shrinks to match. Each chunk weighs its count of
    boxed rows, which is the test's ``r_i``, and its bytes equal its rows.
    """
    total = len(row_boxes)
    chunks: list[tuple[float, float, float, float]] = []
    weights: list[float] = []
    for k in range(_ORDERING_CHUNKS):
        boxed = [
            b
            for b in row_boxes[k * total // _ORDERING_CHUNKS : (k + 1) * total // _ORDERING_CHUNKS]
            if b is not None
        ]
        if boxed:
            chunks.append(bbox_union(boxed))
            weights.append(float(len(boxed)))
    return chunks, weights


def _row_bboxes(
    parquet: Any, geo: dict[str, Any]
) -> list[tuple[float, float, float, float] | None] | None:
    """Per-row [minx, miny, maxx, maxy] from the bbox covering column's values.

    Reads the four covering leaves and not the geometry, so the cost is four
    float64 columns. Returns None when the file has no 1.1 covering column, or
    when its leaves sit deeper than the single struct level the spec uses.

    Rows whose covering values are null are kept in place as ``None`` rather
    than abandoning the file. GeoParquet permits a null geometry, writers give
    those rows a null covering box, and a row with no geometry has no position
    — so it cannot be out of spatial order, and one of them must not cost the
    whole file its ordering check. Keeping its slot keeps the chunk boundaries
    where PORTO-FMT-006's test puts them. Returns None only when no row has a
    box at all.
    """
    columns = geo.get("columns")
    if not isinstance(columns, dict):
        return None
    covering = columns.get(geo.get("primary_column"), {}).get("covering", {}).get("bbox")
    if not isinstance(covering, dict):
        return None
    paths = [covering.get(corner) for corner in ("xmin", "ymin", "xmax", "ymax")]
    if not all(isinstance(p, list) and len(p) == 2 for p in paths):
        return None
    if len({p[0] for p in paths}) != 1:  # type: ignore[index]
        return None
    try:
        flat = parquet.read(columns=[paths[0][0]]).flatten()  # type: ignore[index]
        corners = [flat.column(f"{p[0]}.{p[1]}").to_pylist() for p in paths]  # type: ignore[index]
    except Exception:
        return None
    boxes: list[tuple[float, float, float, float] | None] = []
    for corner_values in zip(*corners, strict=True):
        if any(value is None for value in corner_values):
            boxes.append(None)  # geometry-less row: no position, so nothing to order
            continue
        boxes.append(
            (
                float(corner_values[0]),
                float(corner_values[1]),
                float(corner_values[2]),
                float(corner_values[3]),
            )
        )
    return boxes if any(b is not None for b in boxes) else None


def _ideal_grid_boxes(
    extent: tuple[float, float, float, float], count: int
) -> list[tuple[float, float, float, float]]:
    """The best layout this box count allows: a near-square tiling of the extent.

    The reference the actual layout is judged against, so that "ordered" means
    "as good as this row-group count allows" rather than a fixed figure that only
    holds at one count. A grid is what a perfect space-filling-curve sort
    converges to, and it is generous towards clustered data, which cannot tile
    evenly -- hence a bar of 0.65 of the achievable rate rather than all of it.

    ``ceil(sqrt(count))`` columns, ``ceil(count / cols)`` rows, laid row by row,
    with the last row's cells stretched so it spans the full width. Every cell
    of the extent is then covered. An earlier version emitted ``count`` cells of
    a ``cols x rows`` grid and left ``cols * rows - count`` cells empty; a window
    landing there skipped every reference box, which inflated the achievable
    rate by 13% at three boxes and 4% at five (rashid#174 review, spec point 2).
    """
    if count <= 0:
        return []
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    height = (extent[3] - extent[1]) / rows
    boxes: list[tuple[float, float, float, float]] = []
    for row in range(rows):
        in_row = cols if row < rows - 1 else count - cols * (rows - 1)
        width = (extent[2] - extent[0]) / in_row
        boxes.extend(
            (
                extent[0] + col * width,
                extent[1] + row * height,
                extent[0] + (col + 1) * width,
                extent[1] + (row + 1) * height,
            )
            for col in range(in_row)
        )
    return boxes


def _axis_overlap_share(a0: float, a1: float, g0: float, g1: float, half: float) -> float:
    """Chance a centre coordinate drawn uniformly on ``[a0, a1]`` lands in the
    target ``[g0, g1]`` grown by ``half`` on each side.

    A span with no width puts all its probability on ``a0`` (formats.md: the
    single-coordinate rule), which is also what makes a zero-size axis judge
    on the other axis alone.
    """
    g0, g1 = g0 - half, g1 + half
    if a1 <= a0:
        return 1.0 if g0 <= a0 <= g1 else 0.0
    return min(1.0, max(0.0, (min(a1, g1) - max(a0, g0)) / (a1 - a0)))


def _weighted_skip_rate(
    boxes: list[tuple[float, float, float, float]],
    rows: list[float],
    sizes: list[float],
    fraction: float = _QUERY_FRACTION,
) -> float:
    """Expected share of the file's bytes a query window skips (formats.md:38).

    The window's centre falls in box ``i`` with probability ``rows_i / N``,
    uniform within the box, and the window spans ``fraction`` of each dimension
    of that box, so the query works where the data is and at its scale: one
    far outlier can neither hide a shuffled file behind a stretched extent nor
    fail a sorted one. Each box then counts by its share of the bytes, because
    what a reader saves is bytes, not a count of row groups. The expectation is
    a closed form over the boxes: O(n^2), footer only, no seed, and the same
    number from every implementation. The retired 20-window sample estimated
    it and moved the verdict with its seed on real files.
    """
    if not boxes:
        return 0.0
    total_rows = float(sum(rows))
    total_size = float(sum(sizes))
    read = 0.0
    for j, target in enumerate(boxes):
        hit = 0.0
        for i, source in enumerate(boxes):
            half_w = fraction * (source[2] - source[0]) / 2.0
            half_h = fraction * (source[3] - source[1]) / 2.0
            fx = _axis_overlap_share(source[0], source[2], target[0], target[2], half_w)
            fy = _axis_overlap_share(source[1], source[3], target[1], target[3], half_h)
            hit += rows[i] / total_rows * fx * fy
        read += sizes[j] / total_size * hit
    return max(0.0, 1.0 - read)


@dataclass(frozen=True)
class _PruningScore:
    """How well a layout of boxes prunes, against the best its count allows.

    ``achieved`` and ``achievable`` are expected skip rates, of the boxes as
    laid out and of :func:`_ideal_grid_boxes` with the same count over the same
    extent. ``area_sum`` is the boxes' areas over the extent's: it is reported
    beside the verdict because at high counts the efficiency saturates, and an
    oversized box barely moves it (a 94-group file scored 0.94 with an area sum
    of 4.07; a re-sort brought that to 1.42). It is ``None`` when the extent
    has no area. ``read_profile`` carries the expected read share at the 1%,
    10% and 25% windows (PORTO-FMT-060/061), and ``largest`` the zero-based
    indexes of the up-to-three boxes largest against the extent, so a failure
    can say whether one outlier or the whole file is at fault.
    """

    count: int
    achieved: float
    achievable: float
    area_sum: float | None
    read_profile: tuple[float, ...] = ()
    largest: tuple[int, ...] = ()

    @property
    def efficiency(self) -> float | None:
        """``achieved / achievable``, clipped at 1.0 since clustered data can
        beat the grid; ``None`` where no layout could skip anything.

        The tolerance absorbs the ulp a box-equals-extent hit probability can
        carry, so one row group is undefined here as it is in gpio.
        """
        if self.achievable <= _NO_SKIP:
            return None
        return min(1.0, self.achieved / self.achievable)

    @property
    def below_bar(self) -> bool | None:
        """The verdict, or ``None`` where there is none.

        An undefined efficiency is no verdict, not a failure and not a pass: a
        reader that took ``False`` for "pass" would print one for a layout the
        spec does not judge (PORTO-FMT-054's vectors pin ``null``).
        """
        efficiency = self.efficiency
        if efficiency is None:
            return None
        return efficiency < _MIN_SKIP_EFFICIENCY


def _pruning_score(
    bboxes: list[tuple[float, float, float, float]],
    rows: list[float] | None = None,
    sizes: list[float] | None = None,
) -> _PruningScore:
    """Score a layout of boxes by the share of its bytes a query window skips.

    The extent is the union of the boxes, not the file's declared bbox. The
    reference grid is scored with uniform rows and bytes (PORTO-FMT-053).
    ``rows`` weighs the window placement and ``sizes`` the cost of a hit; a
    layout with no byte counts, such as the row check's chunks, weighs bytes
    by rows (formats.md: ``s_i = r_i``). Callers hold the applicability floors
    themselves; the numbers are defined at any count, and one box scores an
    undefined efficiency because its reference is the extent itself.

    Deliberately not the fraction of consecutive pairs that overlap
    (PORTO-FMT-049): row groups produced by a space-filling-curve sort are
    spatially adjacent by construction, so their boxes touch. For perfectly tiled
    data that fraction runs about 0.75 at thirteen boxes, 0.88 at fifty-nine and
    0.96 at five hundred and eighty-nine -- it is near 1.0 for the best possible
    file, and cannot separate boxes that each span the extent from boxes that
    tile it.
    """
    rows = rows if rows is not None else [1.0] * len(bboxes)
    sizes = sizes if sizes is not None else list(rows)
    extent = bbox_union(bboxes)
    achieved = _weighted_skip_rate(bboxes, rows, sizes)
    reference = _ideal_grid_boxes(extent, len(bboxes))
    achievable = _weighted_skip_rate(reference, [1.0] * len(reference), [1.0] * len(reference))
    extent_area = _bbox_area(extent)
    area_sum = sum(_bbox_area(b) for b in bboxes) / extent_area if extent_area > 0 else None
    profile = tuple(
        1.0 - _weighted_skip_rate(bboxes, rows, sizes, fraction) for fraction in _REPORT_FRACTIONS
    )
    ranked = sorted(range(len(bboxes)), key=lambda i: _bbox_area(bboxes[i]), reverse=True)
    return _PruningScore(len(bboxes), achieved, achievable, area_sum, profile, tuple(ranked[:3]))


def _pruning_summary(
    score: _PruningScore, unit: str, *, judged: bool, bar: float = _MIN_SKIP_EFFICIENCY
) -> str:
    """The measured quantity, for a message, leading with the absolute cost.

    PORTO-FMT-060: the share of the bytes a typical query reads comes first,
    then the achievable rate and the efficiency (with the bar a verdict rests
    on), the 1% and 25% window profile (PORTO-FMT-061), the area sum
    (PORTO-FMT-051), and on a failure the largest boxes (PORTO-FMT-061).
    """
    efficiency = "undefined" if score.efficiency is None else f"{score.efficiency:.2f}"
    verdict = f", under the {bar:.2f} bar" if judged else ""
    text = (
        f"a typical query window, sized {_QUERY_FRACTION:.0%} of the {unit} it lands in, "
        f"reads {1.0 - score.achieved:.0%} of the bytes against "
        f"{1.0 - score.achievable:.0%} for an ideal tiling "
        f"(efficiency {efficiency}{verdict})"
    )
    if len(score.read_profile) == len(_REPORT_FRACTIONS):
        small, _, large = score.read_profile
        text += f"; windows at 1% and 25% read {small:.0%} and {large:.0%}"
    if score.area_sum is not None:
        text += f"; the boxes sum to {score.area_sum:.2f} of the extent"
    if judged and score.below_bar and score.largest:
        named = ", ".join(str(i) for i in score.largest)
        text += f"; the largest boxes are {unit}s {named}, counted from 0"
    return text


def _bbox_area(b: tuple[float, float, float, float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _bbox_overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def geo_from_parquet(files: AssetFiles) -> Geo | None:
    parquet = files.parquet()
    geo = geo_metadata(parquet)
    if geo is None and _native_geometry(parquet) is None:
        return None
    if isinstance(geo, _MalformedGeo):
        # The GeoParquet checks report the malformed spatial metadata.
        return Geo(bbox=None, epsg=None, crs=None)
    if geo is None:
        # The spec's third accepted form: footprint and CRS from the file.
        return _geo_from_native_parquet(parquet) or Geo(bbox=None, epsg=None, crs=None)
    primary = geo.get("primary_column")
    column = geo.get("columns", {}).get(primary, {})
    bbox = column.get("bbox")
    crs_obj = column.get("crs")
    crs = CRS.from_user_input(crs_obj) if crs_obj is not None else CRS.from_epsg(4326)
    return Geo(bbox=as_bbox(bbox), epsg=crs.to_epsg(), crs=crs)


def _geo_from_native_parquet(parquet: Any) -> Geo | None:
    """Footprint and CRS of a native-geo file without a ``geo`` key.

    The bbox is the union of the per-row-group ``GeospatialStatistics`` and the
    CRS comes off the logical type, so the consistency check covers the spec's
    third accepted form too. A CRS this cannot resolve is the native-CRS
    check's business, and the footprint comparison stands down rather than
    assume a datum.
    """
    primary = _primary_geometry_column(parquet, {})
    if primary is None:
        return None
    boxes = _native_bboxes(parquet, {"primary_column": primary})
    if not boxes:
        return None
    bbox = [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]
    crs = _resolve_native_crs(parquet, primary)
    if crs is None:
        return None
    return Geo(bbox=bbox, epsg=crs.to_epsg(), crs=crs)


def _resolve_native_crs(parquet: Any, primary: str | None) -> CRS | None:
    """The column's CRS as pyproj understands it, None when unresolvable."""
    if primary is None:
        return None
    value = _native_crs(parquet, primary)
    if value is None or not value.strip():
        return CRS.from_epsg(4326)  # absent is OGC:CRS84 by definition
    value = value.strip()
    if value.startswith("projjson:"):
        kv = getattr(parquet.metadata, "metadata", None) or {}
        raw = kv.get(value[len("projjson:") :].encode())
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return CRS.from_user_input(value)
    except Exception:
        return None
