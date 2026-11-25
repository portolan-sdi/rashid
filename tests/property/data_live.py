"""Valid data-bearing catalogs, and one breaker per data and live check ID.

Imported only after the caller skips on a missing geospatial stack: it writes
real GeoParquet, COG, and Zarr assets through ``tests.integration._data_assets``
and the Zarr store fixtures.

A base catalog holds one to three collections, each of one kind: ``vector``
(GeoParquet on the collection and on each item), ``raster`` (a COG on each
item), or ``zarr`` (a conforming store). Collection IDs start with the kind.
A breaker that needs a kind the base lacks adds a collection of it first, so a
breaker works on any valid catalog root, including one another strategy wrote.

Live breakers inject :class:`TreeProber`, which answers probes from the tree
on disk as a well-configured host would, so no request leaves the process.
"""

from __future__ import annotations

import json
import shutil
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pyarrow as pa
import pyarrow.parquet as pq
from hypothesis import strategies as st

from rashid_core import ValidationOptions
from rashid_core._media import COG_MEDIA_TYPE, PARQUET_MEDIA_TYPE
from rashid_core.ids import (
    DAT_CHECKSUM,
    DAT_CLASS_COLORS,
    DAT_COG,
    DAT_COG_STATS,
    DAT_COLUMN_DESCRIPTIONS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_GEOPARQUET_VERSION,
    DAT_MIRROR,
    DAT_NATIVE_CRS,
    DAT_ORDERING,
    DAT_OVERVIEW_FORMAT,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SCENE_MASK,
    DAT_SIZE,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_UNAVAILABLE,
    DAT_VALID_PERCENT,
    DAT_VECTOR_COLUMNS,
    LIV_CORS_EXPOSE,
    LIV_CORS_ORIGIN,
    LIV_CORS_PREFLIGHT,
    LIV_HEAD_LENGTH,
    LIV_LINK_TARGET,
    LIV_OFF_HTTPS,
    LIV_RANGE,
    LIV_SELF_BASE,
    LIV_UNAVAILABLE,
    ZAR_ASSET,
    ZAR_AXIS_ORDER,
    ZAR_CHUNK_SIZE,
    ZAR_CODECS,
    ZAR_CONVENTIONS,
    ZAR_CORS,
    ZAR_CRS,
    ZAR_DISPLAY_RANGE,
    ZAR_LAYOUT_FIELDS,
    ZAR_LEVEL_STEP,
    ZAR_NODATA,
    ZAR_NONSPATIAL_CHUNK,
    ZAR_PYRAMID,
    ZAR_READ_AMPLIFICATION,
    ZAR_ROW_MAJOR,
    ZAR_SHARD_SPAN,
    ZAR_STORE_VERSION,
    ZAR_TRANSFORM,
    ZAR_VARIABLES,
)
from rashid_core.live import ProbeResponse
from tests.conftest import CatalogBuilder, CollectionBuilder, mutate_json
from tests.integration import _data_assets as assets
from tests.integration.test_data_catalog import _TABLE_COLUMNS
from tests.integration.test_data_storage import _FLOOD_TABLE, _classified_asset
from tests.property.registry import Breaker, breaks
from tests.unit.test_data_zarr import STORES, zarr_asset

#: Where the live breakers say the catalog is published. TreeProber serves it.
BASE_URL = "https://data.example.com/"

#: The metadata passes say nothing about bytes, so the data and live runs skip them.
_LEAN = ValidationOptions(rules=(), structural=False, extensions=False)


# --- valid catalogs ---------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One collection of a base catalog."""

    kind: str  # "vector", "raster", or "zarr"
    name: str
    items: int = 1
    store: str = "flat"


_NAMES = st.text("abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6)
_SPECS = st.one_of(
    st.builds(Spec, st.just("vector"), _NAMES, st.integers(0, 2)),
    st.builds(Spec, st.just("raster"), _NAMES, st.integers(1, 2)),
    st.builds(Spec, st.just("zarr"), _NAMES, st.just(0), st.sampled_from(["flat", "pyramid"])),
)


def catalog_specs() -> st.SearchStrategy[list[Spec]]:
    """One to three collections with distinct names, of any mix of kinds."""
    return st.lists(_SPECS, min_size=1, max_size=3, unique_by=lambda spec: spec.name)


def write_catalog(root: Path, specs: list[Spec]) -> Path:
    """Write a valid catalog holding ``specs`` at ``root`` and return it."""
    CatalogBuilder(root).write()
    for spec in specs:
        add_collection(root, spec)
    return root


def _asset(href: str, media_type: str) -> dict[str, Any]:
    return {"href": href, "type": media_type, "roles": ["data"]}


def seal(document: Path) -> None:
    """Declare the true file:size and file:checksum of every local file asset."""

    def update(data: dict[str, Any]) -> None:
        for asset in data.get("assets", {}).values():
            href = asset.get("href")
            if isinstance(href, str) and "://" not in href and (document.parent / href).is_file():
                payload = (document.parent / href).read_bytes()
                asset["file:size"] = len(payload)
                asset["file:checksum"] = assets.multihash(payload)

    mutate_json(document, update)


def add_collection(root: Path, spec: Spec) -> Path:
    """Write one valid collection of ``spec.kind``, link it from the root, and return its directory."""
    cid = f"{spec.kind}-{spec.name}"
    directory = root / cid
    title = f"Collection {cid}"
    if spec.kind == "zarr":
        CollectionBuilder(cid, 0, title, assets={"data": zarr_asset()}).write(directory)
        shutil.copytree(STORES / spec.store, directory / "data.zarr")
    else:
        vector = spec.kind == "vector"
        href, media_type = (
            ("./data.parquet", PARQUET_MEDIA_TYPE) if vector else ("./data.tif", COG_MEDIA_TYPE)
        )
        extra: dict[str, Any] = {"table:columns": _TABLE_COLUMNS} if vector else {}
        own = {"data": _asset(href, media_type)} if vector else {}
        collection = CollectionBuilder(cid, 0, title, assets=own, **extra)
        properties: dict[str, Any] = {"datetime": "2024-01-01T00:00:00Z", **extra}
        for index in range(spec.items):
            collection.item(
                f"{cid}-{index}", assets={"data": _asset(href, media_type)}, properties=properties
            )
        collection.write(directory)
        own_dir = [directory] if vector else []
        for holder in [*own_dir, *(directory / item.item_id for item in collection.items)]:
            if vector:
                assets.write_geoparquet(holder / "data.parquet")
            else:
                assets.write_cog(holder / "data.tif", size=256)
        for document in directory.rglob("*.json"):
            seal(document)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {
                "rel": "child",
                "href": f"./{cid}/collection.json",
                "type": "application/json",
                "title": title,
            }
        ),
    )
    return directory


def collection_of(root: Path, kind: str) -> Path:
    """The first collection of ``kind``, added if the catalog has none."""
    found = sorted(root.glob(f"{kind}-*/collection.json"))
    return found[0].parent if found else add_collection(root, Spec(kind, "added"))


def first_item(directory: Path) -> Path:
    """The first item document under a raster collection, which always has one."""
    return min(directory.glob("*/*.json"))


# --- the live host ------------------------------------------------------------


@dataclass(frozen=True)
class TreeProber:
    """A host serving ``root`` under :data:`BASE_URL`; each flag breaks one behavior."""

    root: Path
    down: bool = False
    ranges: bool = True
    origin: bool = True
    expose: bool = True
    preflight_headers: str = (
        "Range, If-Match, If-Modified-Since, If-None-Match, If-Unmodified-Since"
    )
    redirect_http: bool = False
    store_cors: bool = True

    def _file(self, url: str) -> Path | None:
        path = self.root / unquote(url.removeprefix(BASE_URL))
        return path if url.startswith(BASE_URL) and path.is_file() else None

    def _up(self) -> None:
        if self.down:
            raise OSError("connection refused")

    def get_range(self, url: str) -> ProbeResponse:
        self._up()
        headers: dict[str, str] = {}
        if self.origin and (self.store_cors or not url.endswith("/zarr.json")):
            headers["access-control-allow-origin"] = "*"
        if self.expose:
            headers["access-control-expose-headers"] = "Content-Range, Accept-Ranges, ETag"
        if self.ranges:
            headers["accept-ranges"] = "bytes"
        status = 206 if self.ranges else 200
        return ProbeResponse(status=status if self._file(url) else 404, headers=headers)

    def head(self, url: str) -> ProbeResponse:
        self._up()
        if self.redirect_http:
            return ProbeResponse(
                status=301, headers={"location": "http://" + url.removeprefix("https://")}
            )
        path = self._file(url)
        if path is None:
            return ProbeResponse(status=404, headers={})
        return ProbeResponse(status=200, headers={"content-length": str(path.stat().st_size)})

    def preflight(self, url: str) -> ProbeResponse:
        self._up()
        headers = {
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET, HEAD",
            "access-control-allow-headers": self.preflight_headers,
        }
        return ProbeResponse(status=204, headers=headers)


def live_options(prober: TreeProber, *, data: bool = False) -> ValidationOptions:
    """Probe ``prober``'s tree as published under :data:`BASE_URL`."""
    return replace(_LEAN, data=data, live=True, live_prober=prober, live_base_url=BASE_URL)


# --- data breakers ------------------------------------------------------------


def _edit_asset(document: Path, key: str, **fields: Any) -> None:
    mutate_json(document, lambda d: d["assets"][key].update(fields))


def _rewrite(document: Path, write: Any) -> ValidationOptions:
    """Rewrite the bytes of ``document``'s data asset with ``write``, then reseal them."""
    href = load(document)["assets"]["data"]["href"]
    write(document.parent / href)
    seal(document)
    return _LEAN


def load(document: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(document.read_text(encoding="utf-8"))
    return data


def _vector(root: Path) -> Path:
    return collection_of(root, "vector") / "collection.json"


def _raster_item(root: Path) -> Path:
    return first_item(collection_of(root, "raster"))


@breaks(DAT_UNAVAILABLE)
def _unreachable_asset(root: Path) -> ValidationOptions:
    asset = _asset("s3://bucket/data.parquet", PARQUET_MEDIA_TYPE)
    mutate_json(_vector(root), lambda d: d["assets"].__setitem__("upstream", asset))
    return _LEAN


@breaks(DAT_CHECKSUM)
def _wrong_checksum(root: Path) -> ValidationOptions:
    _edit_asset(_vector(root), "data", **{"file:checksum": assets.multihash(b"")})
    return _LEAN


@breaks(DAT_SIZE)
def _wrong_size(root: Path) -> ValidationOptions:
    document = _vector(root)
    size = load(document)["assets"]["data"]["file:size"]
    _edit_asset(document, "data", **{"file:size": size + 1})
    return _LEAN


@breaks(DAT_FORMAT)
def _wrong_media_type(root: Path) -> ValidationOptions:
    _edit_asset(_vector(root), "data", type="application/vnd.pmtiles")
    return _LEAN


@breaks(DAT_CONSISTENCY)
def _bbox_elsewhere(root: Path) -> ValidationOptions:
    mutate_json(_raster_item(root), lambda d: d.__setitem__("bbox", [10.0, 60.0, 12.0, 62.0]))
    return _LEAN


@breaks(DAT_PARTITION_SCHEMA)
def _diverging_partitions(root: Path) -> ValidationOptions:
    document = _vector(root)
    parts = document.parent / "parts"
    parts.mkdir()
    assets.write_geoparquet(parts / "a.parquet")
    assets.write_geoparquet(parts / "b.parquet", columns={"extra": list(range(6))})
    mutate_json(document, lambda d: d.__setitem__("partition:glob", "parts/*.parquet"))
    return _LEAN


@breaks(DAT_TABULAR)
def _undocumented_table(root: Path) -> ValidationOptions:
    document = _vector(root)
    mutate_json(document, lambda d: d.pop("table:columns"))
    return _rewrite(document, lambda path: assets.write_geoparquet(path, geo=False))


@breaks(DAT_MIRROR)
def _mirror_without_ids(root: Path) -> ValidationOptions:
    directory = collection_of(root, "raster")
    mirror = {"href": "./items.parquet", "type": PARQUET_MEDIA_TYPE, "roles": ["collection-mirror"]}
    mutate_json(directory / "collection.json", lambda d: d["assets"].__setitem__("items", mirror))
    assets.write_item_mirror(directory / "items.parquet", None)
    seal(directory / "collection.json")
    return _LEAN


@breaks(DAT_VECTOR_COLUMNS)
def _undocumented_vector(root: Path) -> ValidationOptions:
    mutate_json(_vector(root), lambda d: d.pop("table:columns"))
    return _LEAN


@breaks(DAT_OVERVIEW_FORMAT)
def _single_band_overview(root: Path) -> ValidationOptions:
    directory = collection_of(root, "raster")
    overview = {"href": "./overview.tif", "type": COG_MEDIA_TYPE, "roles": ["visual", "overview"]}
    mutate_json(
        directory / "collection.json", lambda d: d["assets"].__setitem__("overview", overview)
    )
    assets.write_cog(directory / "overview.tif", size=256, count=1)
    seal(directory / "collection.json")
    return _LEAN


@breaks(DAT_COLUMN_DESCRIPTIONS)
def _bare_column(root: Path) -> ValidationOptions:
    mutate_json(_vector(root), lambda d: d["table:columns"][0].pop("description"))
    return _LEAN


@breaks(DAT_CLASS_COLORS)
def _class_color_off_the_table(root: Path) -> ValidationOptions:
    document = _raster_item(root)
    _edit_asset(document, "data", **_classified_asset(**{"permanent-water": "1E90FF"}))
    return _rewrite(
        document, lambda path: assets.write_cog(path, size=256, nodata=0, colormap=_FLOOD_TABLE)
    )


@breaks(DAT_NATIVE_CRS)
def _registry_code_native_crs(root: Path) -> ValidationOptions:
    return _rewrite(_vector(root), lambda path: write_native_crs_geoparquet(path, "srid:3857"))


# Byte rewrites that each break one storage rule, in the shapes the storage tests use.
_GEOPARQUET_REWRITES: dict[str, dict[str, Any]] = {
    DAT_ORDERING: {"points": assets.interleaved_points()},
    DAT_ROWGROUP_STATS: {"covering": False},
    DAT_ROWGROUP_SIZE: {"points": assets.ordered_points(150_001), "row_group_size": 200_000},
    DAT_GEOPARQUET_VERSION: {"version": "1.0.0"},
}
_COG_REWRITES: dict[str, Any] = {
    DAT_COG: assets.write_plain_tiff,
    DAT_COG_STATS: lambda path: assets.write_cog(path, size=256, stats=False),
    DAT_VALID_PERCENT: lambda path: assets.write_cog(path, size=256, valid_percent=False),
    DAT_OVERVIEWS: lambda path: assets.write_cog(path, size=400, blocksize=256, overviews=False),
    DAT_TILE_SIZE: assets.write_tiled_tiff,
    DAT_SCENE_MASK: lambda path: assets.write_cog(path, size=256, count=3),
}


def _geoparquet_breaker(settings: dict[str, Any]) -> Breaker:
    return lambda root: _rewrite(
        _vector(root), lambda path: assets.write_geoparquet(path, **settings)
    )


def _cog_breaker(write: Any) -> Breaker:
    return lambda root: _rewrite(_raster_item(root), write)


for _check_id, _settings in _GEOPARQUET_REWRITES.items():
    breaks(_check_id)(_geoparquet_breaker(_settings))
for _check_id, _write in _COG_REWRITES.items():
    breaks(_check_id)(_cog_breaker(_write))


def write_native_crs_geoparquet(path: Path, crs: str) -> None:
    """A native-geometry Parquet file whose GEOMETRY logical type names ``crs``.

    pyarrow drops an authority-code crs on write but keeps an ``srid:`` one.
    """

    class _Wkb(pa.ExtensionType):
        def __init__(self) -> None:
            super().__init__(pa.binary(), "geoarrow.wkb")

        def __arrow_ext_serialize__(self) -> bytes:
            return f'{{"crs": "{crs}"}}'.encode()

        @classmethod
        def __arrow_ext_deserialize__(cls, storage_type: pa.DataType, serialized: bytes) -> _Wkb:
            return cls()

    points = assets.ordered_points()
    wkb = pa.array([struct.pack("<BIdd", 1, 1, x, y) for x, y in points], type=pa.binary())
    geometry = pa.ExtensionArray.from_storage(_Wkb(), wkb)
    pq.write_table(
        pa.table({"geometry": geometry, "value": list(range(len(points)))}), path, row_group_size=2
    )


# --- Zarr breakers --------------------------------------------------------------

#: Each check and the fixture store that breaks it (see tests/unit/test_data_zarr.py).
_BROKEN_STORES = {
    ZAR_STORE_VERSION: "zarr-v2",
    ZAR_CONVENTIONS: "no-conventions",
    ZAR_TRANSFORM: "no-transform",
    ZAR_AXIS_ORDER: "axis-order",
    ZAR_ROW_MAJOR: "transposed",
    ZAR_CRS: "no-crs",
    ZAR_PYRAMID: "no-pyramid",
    ZAR_LAYOUT_FIELDS: "layout-fields",
    ZAR_LEVEL_STEP: "step-three",
    ZAR_READ_AMPLIFICATION: "time-chunked",
    ZAR_NONSPATIAL_CHUNK: "time-chunked-four",
    ZAR_CHUNK_SIZE: "small-chunks",
    ZAR_SHARD_SPAN: "wide-shard",
    ZAR_CODECS: "unsupported-codec",
    ZAR_VARIABLES: "two-variables",
}

#: Asset edits that break a check against the store the base already has.
_ZARR_ASSET_EDITS: dict[str, dict[str, Any]] = {
    ZAR_ASSET: {"type": "application/vnd.zarr"},
    ZAR_DISPLAY_RANGE: {"bands": [{"name": "temp", "nodata": -9999}]},
    ZAR_NODATA: {"bands": [{"name": "temp", "statistics": {"minimum": 0, "maximum": 1}}]},
    ZAR_VARIABLES: {"bands": [{"name": "temp"}, {"name": "prec"}]},
}


def _zarr_breaker(check_id: str) -> Breaker:
    def breaker(root: Path) -> ValidationOptions:
        directory = collection_of(root, "zarr")
        if check_id in _BROKEN_STORES:
            shutil.rmtree(directory / "data.zarr")
            shutil.copytree(STORES / _BROKEN_STORES[check_id], directory / "data.zarr")
        _edit_asset(directory / "collection.json", "data", **_ZARR_ASSET_EDITS.get(check_id, {}))
        return _LEAN

    return breaker


for _check_id in {**_BROKEN_STORES, **_ZARR_ASSET_EDITS}:
    breaks(_check_id)(_zarr_breaker(_check_id))


# --- live breakers --------------------------------------------------------------

#: Host misbehaviors as TreeProber flags, with the collection kinds each needs:
#: the range and preflight checks probe a partial-read file, which a GeoParquet is.
_HOST_FAULTS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    LIV_UNAVAILABLE: ({"down": True}, ()),
    LIV_RANGE: ({"ranges": False}, ("vector",)),
    LIV_CORS_ORIGIN: ({"origin": False}, ()),
    LIV_CORS_EXPOSE: ({"expose": False}, ("vector",)),
    LIV_CORS_PREFLIGHT: ({"preflight_headers": "If-Match"}, ("vector",)),
    LIV_OFF_HTTPS: ({"redirect_http": True}, ()),
    ZAR_CORS: ({"store_cors": False}, ("zarr",)),
}


def _host_breaker(flags: dict[str, Any], kinds: tuple[str, ...]) -> Breaker:
    def breaker(root: Path) -> ValidationOptions:
        for kind in kinds:
            collection_of(root, kind)
        return live_options(TreeProber(root, **flags))

    return breaker


for _check_id, (_flags, _kinds) in _HOST_FAULTS.items():
    breaks(_check_id)(_host_breaker(_flags, _kinds))


@breaks(LIV_HEAD_LENGTH)
def _stale_size(root: Path) -> ValidationOptions:
    _wrong_size(root)
    return live_options(TreeProber(root))


@breaks(LIV_LINK_TARGET)
def _missing_link_target(root: Path) -> ValidationOptions:
    (root / "README.md").unlink()
    return live_options(TreeProber(root))


@breaks(LIV_SELF_BASE)
def _self_link_elsewhere(root: Path) -> ValidationOptions:
    self_link = {
        "rel": "self",
        "href": "https://elsewhere.example.org/catalog.json",
        "type": "application/json",
    }
    mutate_json(root / "catalog.json", lambda d: d["links"].append(self_link))
    return live_options(TreeProber(root))
