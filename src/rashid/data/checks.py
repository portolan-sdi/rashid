"""Byte-level checks: does an asset's data match what its metadata declares?

Reached only through :func:`rashid.data.default_validator`, so importing this
module (and the geospatial stack it pulls) happens only when the opt-in data
pass actually runs. Each check turns a divergence between the declared metadata
and the real bytes into a :class:`rashid.data.DataDefect`:

- ``PTL-DAT-001`` recomputed multihash ≠ ``file:checksum`` (MUST)
- ``PTL-DAT-002`` byte length ≠ ``file:size`` (MUST)
- ``PTL-DAT-003`` magic bytes ≠ declared media type (MUST)
- ``PTL-DAT-004`` a raster asset is not a valid COG (MUST, formats.md:91)
- ``PTL-DAT-005`` actual bbox/CRS inconsistent with the declared metadata
  (advisory). A ``proj:epsg`` the asset declares itself is faulted on the asset;
  one inherited from the enclosing collection or item is faulted at the field
  that declares it, once, however many assets read it.
- ``PTL-DAT-006`` GeoParquet rows are not spatially ordered (MUST, formats.md:30)
- ``PTL-DAT-007`` no per-row-group spatial statistics (MUST, formats.md:64)
- ``PTL-DAT-008`` a row group exceeds 150,000 rows (MUST, formats.md:75)
- ``PTL-DAT-009`` COG bands lack embedded statistics (MUST, formats.md:95)
- ``PTL-DAT-010`` a band lacks embedded valid percent (SHOULD; MUST — and thus
  an ERROR — when the band has a nodata value, formats.md:121)
- ``PTL-DAT-011`` a raster larger than its own internal tile has no internal
  overviews. OGC 21-026 makes overviews optional in base COG but a SHALL in
  its Optimized GeoTIFF conformance class (/req/optimized_geotiff) — the class
  Portolan's efficient-range-request mandate targets. ``cog_validate`` checks
  base COG, so it accepts such a file with only a warning; without overviews a
  zoomed-out render reads every full-resolution byte.
- ``PTL-DAT-012`` GeoParquet version is not 1.1 or 2.x (MUST, formats.md:25)
- ``PTL-DAT-013`` internal tiles are not square or exceed 512px (MUST,
  OGC 21-026 /req/optimized_geotiff/small-sizes via formats.md:121)
- ``PTL-DAT-014`` partition files diverge in Parquet schema (MUST,
  formats.md:91: "validated by tooling reading file footers")
- ``PTL-DAT-015`` a tabular (plain Parquet) collection is missing the SHOULDs
  of formats.md, Tabular Data: ``table:columns`` documenting the columns, and
  ``extent.temporal`` when the file carries a temporal column (WARNING)
- ``PTL-DAT-016`` an item mirror diverges from the collection's items in row
  count, ids, geometry, datetime, or bbox (MUST, formats.md, Raster § Item
  mirror). A mirror runs the GeoParquet checks above as well: the spec binds
  it to them like any other spatial table.

The four GeoParquet checks — ``PTL-DAT-006``, ``007``, ``008``, and ``012`` —
run over an asset and over every file a local relative ``partition:glob``
matches. A partitioned collection publishes its data through the glob and
declares no ``data`` asset, so the glob is the only place those bytes appear.
Each rule reports once per collection, at ``/partition:glob``, naming how many
partition files failed.

``PTL-DAT-007`` also carries formats.md's covering-column recommendation: a
GeoParquet 2.x file that satisfies the statistics MUST through native
``GeospatialStatistics`` alone, without the RECOMMENDED ``bbox`` covering
column, gets a WARNING rather than an ERROR.

The ``STATISTICS_APPROXIMATE`` MUST-when-estimated cannot be checked from the
bytes: whether the statistics were estimated is not knowable after the fact.

Checks that cannot run for a given asset — bytes unreachable, an unsupported
hash function, an unreadable header — degrade to an INFO or to silence rather
than a false ERROR; a present-but-unverifiable checksum is not a conformance
failure the way a wrong one is.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import rasterio

from rashid import _trace
from rashid.catalog import CatalogGraph, Node
from rashid.data import DataDefect, DataResults
from rashid.data._files import AssetFiles
from rashid.data.geoparquet import (
    check_geoparquet,
    check_tabular,
    check_vector_columns,
    geo_from_parquet,
)
from rashid.data.mirror import check_mirror
from rashid.data.partitions import check_partitions
from rashid.data.raster import check_raster, geo_from_raster
from rashid.data.reader import AssetReader
from rashid.data.spatial import (
    Geo,
    Wgs84Bounds,
    bbox_mismatch_message,
    bbox_within,
    check_collection_extent,
    declared_bbox,
    declared_epsg,
    epsg_defect,
    geo_from_pmtiles,
    unreadable_spatial_metadata,
    wgs84_bounds,
)
from rashid.data.storage import check_bytes, expected_format
from rashid.model import Severity
from rashid.registry import (
    DAT_CONSISTENCY,
)
from rashid.rules._common import (
    asset_refers_to_self,
    assets_of,
    is_source_asset,
)
from rashid.rules.item_mirror import is_mirror_asset

# GDAL options for a remote raster (https://gdal.org/en/stable/user/configoptions.html).
# On a published host each request waits hundreds of milliseconds for its
# first byte (#219), so these cut requests rather than bytes:
# - EMPTY_DIR stops GDAL from listing the asset's directory and probing for
#   sidecar files. A remote COG's external .ovr then goes unseen by
#   cog_validate; PTL-DAT-011 still reports the missing internal overviews.
# - VSI_CACHE keeps the bytes already read in RAM for the one open dataset the
#   stats, overview, tile, and extent reads share.
# - GDAL_HTTP_MAX_RETRY retries a 429, 500, 502, 503, or 504, as rashid._store
#   retries obstore's requests. GDAL's default is no retry. The data pass
#   reads 32 assets at once, and a host that throttles answers 503 now and
#   then. A raster open that fails on one would report a valid COG as broken.
#   The first retry waits 0.5 s, and GDAL grows the wait after each one.
# GDAL_HTTP_MERGE_CONSECUTIVE_RANGES is YES by default, so it is not set.
# Local files keep GDAL's defaults, which is how a local .ovr stays visible.
# rasterio sets these thread-locally on a worker thread.
_REMOTE_GDAL = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "VSI_CACHE": "TRUE",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "0.5",
}


log = logging.getLogger(__name__)


def check_node(
    node: Node, reader: AssetReader, graph: CatalogGraph | None = None
) -> list[DataDefect]:
    """Verify every asset on ``node`` against its declared metadata.

    ``graph`` is optional because one check, the item mirror's agreement with
    the collection's items, needs the object's children. Without it that check
    is skipped.
    """
    results = DataResults()
    bounds: list[Wgs84Bounds] = []
    extent_complete = True
    schemas, partitions, total = check_partitions(node, reader)
    results.extend(schemas)
    results.extend(partitions, partition_total=total)
    for _pointer, key, asset in assets_of(node):
        href = asset.get("href")
        if not isinstance(href, str) or not href:
            media_type = asset.get("type")
            spatial = (
                not is_source_asset(asset)
                and isinstance(media_type, str)
                and expected_format(media_type) in {"parquet", "tiff", "pmtiles"}
            )
            extent_complete = extent_complete and not spatial
            continue  # PTL-AST-001 reports a missing href
        if asset_refers_to_self(node, href, graph):
            continue  # PTL-AST-003 requires both file metadata fields to be absent
        log.debug("data: %s asset %r: %s", node.path, key, href)
        with _trace.span("data.asset"):
            defects, actual, complete = _check_asset(
                node, key=key, asset=asset, reader=reader, graph=graph
            )
            results.extend(defects)
            extent_complete = extent_complete and complete
            if actual is not None:
                bounds.append(actual)
    results.extend(check_collection_extent(node, bounds, complete=extent_complete))
    return results.build()


def _check_asset(
    node: Node,
    *,
    key: str,
    asset: dict[str, Any],
    reader: AssetReader,
    graph: CatalogGraph | None = None,
) -> tuple[list[DataDefect], Wgs84Bounds | None, bool]:
    href = asset["href"]
    _trace.count("data.assets")
    defects: list[DataDefect] = []
    media_type = asset.get("type")
    expected = expected_format(media_type) if isinstance(media_type, str) else None

    defects.extend(check_bytes(key, asset=asset, expected=expected, node=node, reader=reader))

    located = reader.locate(node, href)
    if located is None or is_source_asset(asset):
        # PORTO-FMT-045: the format requirements apply to the catalog's own
        # cloud-native assets, and a source asset is not one. It names the
        # upstream original a cloud-native asset was derived from, which is a
        # Shapefile or a GeoPackage as often as not, so judging it as a COG or
        # a GeoParquet would fail it for being what it says it is. Its bytes
        # are still checksum/size/format-verified above.
        return (
            defects,
            None,
            is_source_asset(asset) or expected not in {"parquet", "tiff", "pmtiles"},
        )
    bounds = None
    complete = True
    gdal_env = rasterio.Env(**_REMOTE_GDAL) if located.is_remote else contextlib.nullcontext()
    with gdal_env, AssetFiles(located) as files:
        if is_mirror_asset(asset):
            # formats.md, Raster § Item mirror: a mirror must reproduce the
            # items it mirrors (PORTO-FMT-042). It then falls through to the
            # GeoParquet checks below, which bind it as they bind vector data
            # (PORTO-FMT-043) — an item index is queried by extent like any
            # other spatial table. The tabular SHOULDs skip it on their own,
            # being scoped to assets with the 'data' role.
            defects.extend(check_mirror(node, key, files, graph))
        if expected == "tiff":
            defects.extend(check_raster(key, located, files))
        if expected == "parquet":
            defects.extend(check_geoparquet(key, files))
            defects.extend(check_tabular(node, key, asset, files))
            defects.extend(check_vector_columns(node, key, asset, files))
        if expected in {"parquet", "tiff", "pmtiles"}:
            consistency, bounds, complete = _check_consistency(
                node, key=key, asset=asset, expected=expected, files=files
            )
            defects.extend(consistency)
    return defects, bounds, complete


def _check_consistency(
    node: Node,
    *,
    key: str,
    asset: dict[str, Any],
    expected: str,
    files: AssetFiles,
) -> tuple[list[DataDefect], Wgs84Bounds | None, bool]:
    try:
        geo = _extract_geo(expected, files)
    except Exception as exc:  # unreadable header is advisory, not fatal
        return [unreadable_spatial_metadata(key, exc)], None, False
    if geo is None:
        return [], None, expected == "parquet"

    defects: list[DataDefect] = []
    epsg_claim = declared_epsg(node, asset)
    if epsg_claim is not None and geo.epsg is not None and epsg_claim.value != geo.epsg:
        defects.append(epsg_defect(node, key, epsg_claim, geo.epsg))

    bbox_claim = declared_bbox(node)
    bounds = wgs84_bounds(geo)
    if (
        node.kind != "collection"
        and bbox_claim is not None
        and bounds is not None
        and not bbox_within(bbox_claim, bounds)
    ):
        message = bbox_mismatch_message(key, bbox_claim, bounds, geo.epsg)
        defects.append(DataDefect(DAT_CONSISTENCY, Severity.WARNING, message, key))
    return defects, bounds, bounds is not None


# --- spatial extraction ----------------------------------------------------


def _extract_geo(expected: str, files: AssetFiles) -> Geo | None:
    if expected == "parquet":
        return geo_from_parquet(files)
    if expected == "tiff":
        return geo_from_raster(files)
    if expected == "pmtiles":
        return geo_from_pmtiles(files.located)
    return None
