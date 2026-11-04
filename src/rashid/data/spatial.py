"""Shared spatial metadata, bounds, and CRS comparisons."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pyproj import CRS, Transformer

from rashid.catalog import Node
from rashid.data import DataDefect
from rashid.data.reader import Locator
from rashid.data.storage import PMTILES_MAGIC
from rashid.model import Severity, json_pointer
from rashid.registry import (
    DAT_CONSISTENCY,
)

# bbox comparison tolerance in degrees (~1 km); absorbs rounding and
# reprojection gridding so only genuine divergence trips PTL-DAT-005.
_BBOX_TOL = 0.01

# Intermediate points per bbox edge when reprojecting. A projected rectangle's
# edges curve in WGS84, and their extremes usually fall between the corners, so
# every edge is walked rather than sampled at its ends.
_DENSIFY_PTS = 21


@dataclass(frozen=True)
class _EpsgDeclaration:
    """A ``proj:epsg`` value and the document node that carries it.

    ``pointer`` is ``None`` when the asset declares the value itself. Otherwise
    the asset inherits it from the enclosing collection or item, and ``pointer``
    locates the single field that governs every asset on that object.
    """

    value: int
    pointer: str | None


@dataclass(frozen=True)
class Geo:
    """Actual spatial metadata read from an asset's bytes."""

    bbox: list[float] | None
    epsg: int | None
    crs: Any | None  # pyproj CRS, when EPSG alone doesn't capture it
    # Whether ``bbox`` is the tight envelope of the data or merely contains it.
    # A geometry column's bbox is tight, so the data reaches every edge; a raster
    # grid only contains its data, and a nodata collar can be wide.
    tight: bool = True


@dataclass(frozen=True)
class Wgs84Bounds:
    """Where the WGS84 envelope of an asset's data must lie.

    ``outer`` contains that envelope. ``inner``, when known, is contained by it,
    so together they pin each side from opposite directions. A bracket is the
    most a header-only check can say about a projected asset: under a non-affine
    projection the native rectangle maps to a curved quadrilateral, whose
    bounding box is a strict superset of the envelope of the data inside it, so
    neither box alone is the answer. An unprojected asset has no distortion to
    absorb and the two boxes coincide.

    ``inner`` is ``None`` when the native bbox was not tight to begin with, which
    leaves plain containment in ``outer`` as the only sound assertion.
    """

    outer: list[float]
    inner: list[float] | None
    reprojected: bool = False


def unreadable_spatial_metadata(
    key: str, error: Exception | str, *, subject: str | None = None
) -> DataDefect:
    subject = subject or f"asset '{key}'"
    return DataDefect(
        DAT_CONSISTENCY,
        Severity.INFO,
        f"{subject} spatial metadata could not be read ({error})",
        key,
    )


def check_collection_extent(
    node: Node, bounds: list[Wgs84Bounds], *, complete: bool = True
) -> list[DataDefect]:
    declared = declared_bbox(node)
    if node.kind != "collection" or not bounds or declared is None:
        return []
    inners = [box.inner for box in bounds if box.inner is not None]
    outers = [box.outer for box in bounds]
    if not complete:
        # Unread assets can extend the union; only its known inner bound can contradict the extent.
        outers.append(declared)
    union = Wgs84Bounds(
        outer=list(bbox_union(outers)),
        inner=list(bbox_union(inners)) if inners else None,
        reprojected=any(box.reprojected for box in bounds),
    )
    if bbox_within(declared, union):
        return []
    return [
        DataDefect(
            DAT_CONSISTENCY,
            Severity.WARNING,
            f"collection '{node.id}' declared bbox {_fmt_bbox(declared)} is inconsistent with "
            f"its asset union {_fmt_bbox(union.outer)}",
            "",
            json_pointer=json_pointer("extent", "spatial", "bbox", 0),
        )
    ]


def epsg_defect(node: Node, key: str, declared: _EpsgDeclaration, actual: int) -> DataDefect:
    """Fault the document node that carries the declaration, not its inheritor.

    An asset that declares its own ``proj:epsg`` and contradicts its own bytes
    is faulted on the asset: the two claims are about the same object, and only
    the publisher knows which one is true. A value inherited from the enclosing
    collection or item is faulted where it is written, and the message says the
    assets inherit it, because the field an editor must change is that one and
    the asset the reader was sent to holds no ``proj:epsg`` at all. The file's
    own spatial metadata settles the inherited case, so ``expected`` carries the
    CRS the bytes declare and ``actual`` the stale value in the document.
    """
    if declared.pointer is None:
        return DataDefect(
            DAT_CONSISTENCY,
            Severity.WARNING,
            f"asset '{key}' declares proj:epsg {declared.value} but its data is EPSG:{actual}",
            key,
            expected=actual,
            actual=declared.value,
        )
    where = (
        "in its properties"
        if declared.pointer.startswith(json_pointer("properties"))
        else "at its root"
    )
    return DataDefect(
        DAT_CONSISTENCY,
        Severity.WARNING,
        f"{node.kind} '{node.id}' declares proj:epsg {declared.value} {where}, which its "
        f"assets inherit, but the asset data is EPSG:{actual}",
        key,
        json_pointer=declared.pointer,
        expected=actual,
        actual=declared.value,
    )


def bbox_mismatch_message(
    key: str, declared: list[float], bounds: Wgs84Bounds, epsg: int | None
) -> str:
    """Say what the declared bbox had to satisfy, not just that it failed."""
    if bounds.inner is None:
        return (
            f"asset '{key}' declared bbox {_fmt_bbox(declared)} is not contained by the "
            f"asset's extent {_fmt_bbox(bounds.outer)}"
        )
    if not bounds.reprojected:
        return (
            f"asset '{key}' data bbox {_fmt_bbox(bounds.outer)} does not match the "
            f"declared bbox {_fmt_bbox(declared)}"
        )
    return (
        f"asset '{key}' declared bbox {_fmt_bbox(declared)} is inconsistent with its "
        f"EPSG:{epsg} data, whose envelope contains {_fmt_bbox(bounds.inner)} and "
        f"lies within {_fmt_bbox(bounds.outer)}"
    )


def bbox_union(
    bboxes: Sequence[Sequence[float]],
) -> tuple[float, float, float, float]:
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def geo_from_pmtiles(located: Locator) -> Geo | None:
    with located.open_binary() as handle:
        header = handle.read(127)
    if len(header) < 127 or header[:7] != PMTILES_MAGIC:
        return None
    # v3 header: min/max lon/lat are int32 E7 at byte offsets 102, 106, 110, 114.
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<iiii", header, 102)
    bbox = [min_lon / 1e7, min_lat / 1e7, max_lon / 1e7, max_lat / 1e7]
    return Geo(bbox=bbox, epsg=4326, crs=CRS.from_epsg(4326))


def as_bbox(bbox: Any) -> list[float] | None:
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None
    try:
        values = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return None
    # Drop any z coordinates: [minx, miny, (minz,) maxx, maxy, (maxz)].
    if len(values) == 6:
        return [values[0], values[1], values[3], values[4]]
    return values[:4]


def wgs84_bounds(geo: Geo) -> Wgs84Bounds | None:
    """Bracket the WGS84 envelope of the data behind a native bbox."""
    if geo.bbox is None:
        return None
    if geo.crs is None or geo.epsg == 4326:
        return Wgs84Bounds(outer=geo.bbox, inner=geo.bbox if geo.tight else None)
    transformer = Transformer.from_crs(geo.crs, CRS.from_epsg(4326), always_xy=True)
    minx, miny, maxx, maxy = geo.bbox[:4]
    outer = list(transformer.transform_bounds(minx, miny, maxx, maxy, densify_pts=_DENSIFY_PTS))
    if not all(math.isfinite(v) for v in outer):
        return None
    if outer[0] > outer[2]:
        # transform_bounds signals an antimeridian crossing by returning a box
        # that wraps; no plain min/max comparison means anything across the seam
        return None
    inner = _inner_bounds(transformer, geo.bbox) if geo.tight else None
    if geo.tight and inner is None:
        return None
    return Wgs84Bounds(outer=outer, inner=inner, reprojected=True)


def _inner_bounds(transformer: Transformer, bbox: list[float]) -> list[float] | None:
    """The largest box the reprojected envelope is guaranteed to *contain*.

    A native bbox is tight, so some geometry touches each of its four edges.
    Reprojecting one edge therefore pins one side of the true envelope from the
    inside: the touching point's longitude is at most that edge's largest
    longitude, so the envelope's own minimum longitude is at most that too, and
    symmetrically for the other three sides. Taking the tightest such bound over
    all four edges holds however the projection reorients the rectangle.
    """
    minx, miny, maxx, maxy = bbox[:4]
    steps = [i / (_DENSIFY_PTS + 1) for i in range(_DENSIFY_PTS + 2)]
    min_lon_at_most = min_lat_at_most = math.inf
    max_lon_at_least = max_lat_at_least = -math.inf
    for x0, y0, x1, y1 in (
        (minx, miny, minx, maxy),
        (maxx, miny, maxx, maxy),
        (minx, miny, maxx, miny),
        (minx, maxy, maxx, maxy),
    ):
        lons, lats = transformer.transform(
            [x0 + (x1 - x0) * f for f in steps], [y0 + (y1 - y0) * f for f in steps]
        )
        if not all(math.isfinite(v) for v in (*lons, *lats)):
            return None
        min_lon_at_most = min(min_lon_at_most, max(lons))
        min_lat_at_most = min(min_lat_at_most, max(lats))
        max_lon_at_least = max(max_lon_at_least, min(lons))
        max_lat_at_least = max(max_lat_at_least, min(lats))
    return [min_lon_at_most, min_lat_at_most, max_lon_at_least, max_lat_at_least]


def bbox_within(declared: list[float], bounds: Wgs84Bounds) -> bool:
    """Does a declared bbox sit inside the bracket, side by side?

    Every side must land inside ``outer``. When ``inner`` is known each side must
    also reach at least that far, which is what keeps an under-declared bbox
    detectable; where the two boxes coincide the pair collapses to plain equality
    within the tolerance.
    """
    minx, miny, maxx, maxy = declared[:4]
    west, south, east, north = bounds.outer
    if not all(west - _BBOX_TOL <= v <= east + _BBOX_TOL for v in (minx, maxx)):
        return False
    if not all(south - _BBOX_TOL <= v <= north + _BBOX_TOL for v in (miny, maxy)):
        return False
    if bounds.inner is None:
        return True
    reach_w, reach_s, reach_e, reach_n = bounds.inner
    return (
        minx <= reach_w + _BBOX_TOL
        and miny <= reach_s + _BBOX_TOL
        and maxx >= reach_e - _BBOX_TOL
        and maxy >= reach_n - _BBOX_TOL
    )


def _fmt_bbox(bbox: list[float]) -> str:
    return "[" + ", ".join(f"{v:.4f}" for v in bbox[:4]) + "]"


def declared_bbox(node: Node) -> list[float] | None:
    if node.kind == "item":
        return as_bbox(node.data.get("bbox"))
    extent = node.data.get("extent", {})
    spatial = extent.get("spatial", {}) if isinstance(extent, dict) else {}
    boxes = spatial.get("bbox") if isinstance(spatial, dict) else None
    if isinstance(boxes, list) and boxes:
        return as_bbox(boxes[0])
    return None


def declared_epsg(node: Node, asset: dict[str, Any]) -> _EpsgDeclaration | None:
    """Find the ``proj:epsg`` an asset is governed by, and where it is written.

    The asset's own value wins, then the enclosing object's ``properties``, then
    its document root. The two outer sources are inherited: one field governs
    every asset on the object, and that field is the one an editor changes.
    """
    sources: tuple[tuple[Any, str | None], ...] = (
        (asset, None),
        (node.data.get("properties", {}), json_pointer("properties", "proj:epsg")),
        (node.data, json_pointer("proj:epsg")),
    )
    for source, pointer in sources:
        if isinstance(source, dict):
            value = source.get("proj:epsg")
            if isinstance(value, int) and not isinstance(value, bool):
                return _EpsgDeclaration(value, pointer)
    return None
