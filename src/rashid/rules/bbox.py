"""Bounding-box sanity rules (spec: core.md, Bounding Boxes and Spatial Extent)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from rashid.catalog import CatalogGraph, Node
from rashid.model import Finding, Severity
from rashid.rule import Rule

# DBL_MAX and friends written out by broken exporters as "effectively
# infinite" sentinels. Anything at or beyond this magnitude is garbage.
_SENTINEL_MAGNITUDE = 1.7e308


def _bboxes_of(node: Node) -> list[tuple[str, Any]]:
    """All bbox arrays on a node, with a JSON pointer for each."""
    found: list[tuple[str, Any]] = []
    if node.kind == "item":
        if "bbox" in node.data:
            found.append(("/bbox", node.data["bbox"]))
        return found
    extent = node.data.get("extent")
    if isinstance(extent, dict):
        spatial = extent.get("spatial")
        if isinstance(spatial, dict):
            boxes = spatial.get("bbox")
            if isinstance(boxes, list):
                for index, box in enumerate(boxes):
                    found.append((f"/extent/spatial/bbox/{index}", box))
    return found


class BboxValidRule(Rule):
    """Every bbox is finite, sentinel-free, in WGS84 range, with south <= north."""

    id = "PTL-BBX-001"
    spec_ids = ("PORTO-CORE-041",)
    default_severity = Severity.ERROR
    description = "bboxes must be finite WGS84 coordinates with south <= north"
    kinds = ("catalog", "collection", "item")

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        for pointer, box in _bboxes_of(node):
            yield from self._check_bbox(node, pointer, box)

    def _check_bbox(self, node: Node, pointer: str, box: Any) -> Iterable[Finding]:
        if not isinstance(box, list) or len(box) not in (4, 6):
            yield self.finding(
                node,
                f"bbox must be an array of 4 or 6 numbers, got {box!r}",
                json_pointer=pointer,
                fix_hint="replace the value with [west, south, east, north] in WGS84 degrees"
                " (or [west, south, min_elev, east, north, max_elev])",
                actual=box,
            )
            return
        values: list[float] = []
        for value in box:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                yield self.finding(
                    node,
                    f"bbox contains a non-numeric value: {value!r}",
                    json_pointer=pointer,
                    fix_hint="replace the entry with an unquoted WGS84 degree value",
                    actual=value,
                )
                return
            values.append(float(value))
        for value in values:
            if math.isnan(value) or math.isinf(value):
                yield self.finding(
                    node,
                    f"bbox contains a NaN or infinite value: {value!r}",
                    json_pointer=pointer,
                    fix_hint="recompute the bbox from the data's geometries and write finite"
                    " WGS84 degrees",
                )
                return
            if abs(value) >= _SENTINEL_MAGNITUDE:
                yield self.finding(
                    node,
                    f"bbox contains an effectively-infinite sentinel value: {value!r}",
                    json_pointer=pointer,
                    fix_hint="recompute the bbox from the data's geometries; the exporter wrote"
                    " a placeholder instead of a real extent",
                    actual=value,
                )
                return
        if len(values) == 4:
            west, south, east, north = values
            elevations: tuple[float, float] | None = None
        else:
            west, south, zmin, east, north, zmax = values
            elevations = (zmin, zmax)
        for name, lon in (("west", west), ("east", east)):
            if not -180 <= lon <= 180:
                yield self.finding(
                    node,
                    f"bbox {name} longitude {lon} outside WGS84 range [-180, 180]",
                    json_pointer=pointer,
                    fix_hint="reproject the extent to EPSG:4326; bbox coordinates are decimal"
                    " degrees, not projected units",
                    actual=lon,
                )
        for name, lat in (("south", south), ("north", north)):
            if not -90 <= lat <= 90:
                yield self.finding(
                    node,
                    f"bbox {name} latitude {lat} outside WGS84 range [-90, 90]",
                    json_pointer=pointer,
                    fix_hint="reproject the extent to EPSG:4326; bbox coordinates are decimal"
                    " degrees, not projected units",
                    actual=lat,
                )
        if south > north:
            yield self.finding(
                node,
                f"bbox south ({south}) is greater than north ({north})",
                json_pointer=pointer,
                fix_hint="swap the two latitudes; bbox order is [west, south, east, north]",
                expected=[north, south],
                actual=[south, north],
            )
        # west > east is legal: it means the bbox crosses the antimeridian.
        if elevations is not None and elevations[0] > elevations[1]:
            yield self.finding(
                node,
                f"bbox minimum elevation ({elevations[0]}) exceeds maximum ({elevations[1]})",
                json_pointer=pointer,
                fix_hint="swap the two elevations; 6-element bbox order is"
                " [west, south, min_elev, east, north, max_elev]",
                expected=[elevations[1], elevations[0]],
                actual=[elevations[0], elevations[1]],
            )
