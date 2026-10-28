"""Seeded catalogs at two sizes, for counter tests, memory ceilings, and profiling.

The fixtures in ``conftest.py`` are sized to prove a rule right. They are too
small to show how a cost grows. Each scenario here builds the same bytes for
the same name, size, and seed, so a counter or a timing taken today compares
with one taken next month. Two sizes show whether a cost grows with the
catalog or stays flat.

``metadata``
    A catalog of collections and items. Every item has a small data asset
    whose size and checksum match its bytes. Validated with ``data=False``,
    it exercises the graph load, the rules, and the structural and extension
    passes.
``data``
    A collection whose items carry real GeoParquet and COG files. It
    exercises the data pass: hashing, the parquet footers, and the rasters.
``remote``
    The ``metadata`` tree served over a local HTTPS server, validated by URL.
    It exercises the crawl pool, the live HEAD pool, and ``rashid._store``.
``remote-data``
    The ``data`` tree served over a local HTTPS server, validated by URL with
    the data pass on. Every asset is remote, as on a published catalog, and
    each asset request waits ``REMOTE_LATENCY`` before the server answers.
    It exercises the remote reads of the data pass: obstore for the bytes
    and the Parquet footers, GDAL ``/vsicurl`` for the rasters.

``scripts/profile.py`` runs these. Built trees are cached under
``.cache/scenarios``; bump ``GENERATOR_VERSION`` when the output changes.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from tests.conftest import CatalogBuilder, mutate_json

GENERATOR_VERSION = 1

NAMES = ("metadata", "data", "remote", "remote-data")

#: Seconds each asset request in ``remote-data`` waits before the answer. A
#: published host takes 0.3 to 0.9 s to a first byte (#219); this keeps the
#: shape of that cost at a scale a test run can afford.
REMOTE_LATENCY = 0.02

#: Which tree each scenario builds. The remote scenarios serve a local tree.
_TREES = {"metadata": "metadata", "data": "data", "remote": "metadata", "remote-data": "data"}
SIZES = ("small", "large")

#: ``metadata``: (collections, items per collection).
_METADATA_SHAPE = {"small": (2, 50), "large": (20, 100)}
#: ``data``: items of each kind (GeoParquet, COG).
_DATA_SHAPE = {"small": 2, "large": 20}

_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "scenarios"

_PARQUET_TYPE = "application/vnd.apache.parquet"
_COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
_TABLE_COLUMNS = [
    {"name": "geometry", "type": "byte_array", "description": "WKB point"},
    {"name": "value", "type": "int64", "description": "a per-row value"},
]


@dataclass(frozen=True)
class Target:
    """What to validate: a path or URL, and the keyword arguments for ``validate``.

    ``server`` is the local HTTPS server of a remote scenario, whose ``seen``
    and ``peak_in_flight`` a test reads after the run.
    """

    location: Path | str
    options: dict[str, Any] = field(default_factory=dict)
    server: Any = None


def _multihash(payload: bytes) -> str:
    return "1220" + hashlib.sha256(payload).hexdigest()


def _sized_asset(href: str, media_type: str, roles: list[str], payload: bytes) -> dict[str, Any]:
    return {
        "href": href,
        "type": media_type,
        "roles": roles,
        "file:size": len(payload),
        "file:checksum": _multihash(payload),
    }


def _write_metadata(root: Path, size: str, rng: random.Random) -> None:
    collections, items = _METADATA_SHAPE[size]
    cat = CatalogBuilder(root)
    payloads: dict[Path, bytes] = {}
    for c in range(collections):
        cid = f"c{c:03d}"
        data = rng.randbytes(1024)
        thumb = rng.randbytes(512)
        payloads[root / cid / "data.parquet"] = data
        payloads[root / cid / "thumbnail.png"] = thumb
        col = cat.collection(
            cid,
            assets={
                "data": _sized_asset("./data.parquet", _PARQUET_TYPE, ["data"], data),
                "thumbnail": _sized_asset("./thumbnail.png", "image/png", ["thumbnail"], thumb),
            },
        )
        for i in range(items):
            iid = f"{cid}-i{i:04d}"
            x, y = round(rng.uniform(4.0, 6.0), 6), round(rng.uniform(50.0, 52.0), 6)
            day = rng.randrange(366)
            body = rng.randbytes(rng.randrange(256, 2048))
            payloads[root / cid / iid / "data.parquet"] = body
            col.item(
                iid,
                geometry={"type": "Point", "coordinates": [x, y]},
                bbox=[x, y, x, y],
                properties={"datetime": f"{date(2024, 1, 1) + timedelta(days=day)}T00:00:00Z"},
                assets={"data": _sized_asset("./data.parquet", _PARQUET_TYPE, ["data"], body)},
            )
    cat.write()
    for path, payload in payloads.items():
        path.write_bytes(payload)


def _write_data(root: Path, size: str, rng: random.Random) -> None:
    # The geospatial stack loads only for this scenario.
    from tests.integration import _data_assets as assets

    per_kind = _DATA_SHAPE[size]
    cat = CatalogBuilder(root)
    col = cat.collection(
        "layers",
        assets={"thumbnail": _sized_asset("./thumbnail.png", "image/png", ["thumbnail"], b"")},
        **{"table:columns": _TABLE_COLUMNS},
    )
    files: list[tuple[str, Path]] = []
    for i in range(per_kind):
        iid = f"points-{i:03d}"
        col.item(
            iid,
            assets={"data": {"href": "./data.parquet", "type": _PARQUET_TYPE, "roles": ["data"]}},
            properties={"datetime": "2024-01-01T00:00:00Z", "table:columns": _TABLE_COLUMNS},
        )
        files.append((iid, root / "layers" / iid / "data.parquet"))
    for i in range(per_kind):
        iid = f"raster-{i:03d}"
        col.item(iid, assets={"data": {"href": "./data.tif", "type": _COG_TYPE, "roles": ["data"]}})
        files.append((iid, root / "layers" / iid / "data.tif"))
    cat.write()
    (root / "layers" / "thumbnail.png").write_bytes(b"")
    for iid, path in files:
        if path.suffix == ".parquet":
            points = assets.hilbert_sorted(assets.local_points(64, seed=rng.randrange(2**31)))
            assets.write_geoparquet(path, points=points, row_group_size=16)
        else:
            assets.write_cog(path, size=512)
        payload = path.read_bytes()
        mutate_json(
            path.parent / f"{iid}.json",
            lambda d, payload=payload: d["assets"]["data"].update(
                {"file:size": len(payload), "file:checksum": _multihash(payload)}
            ),
        )


def build(name: str, size: str, dest: Path, seed: int = 0) -> Path:
    """Write scenario ``name`` at ``size`` into ``dest``, which must not exist."""
    if name not in NAMES:
        raise ValueError(f"scenario must be one of {NAMES}, got {name!r}")
    if size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    # A remote scenario serves a local tree; the same seed gives the same tree.
    tree = _TREES[name]
    rng = random.Random(f"{tree}-{size}-{seed}")
    if tree == "data":
        _write_data(dest, size, rng)
    else:
        _write_metadata(dest, size, rng)
    return dest


def cached(name: str, size: str, seed: int = 0, cache_dir: Path | None = None) -> Path:
    """The scenario's tree, built once into ``cache_dir`` and reused after that."""
    cache_dir = cache_dir or _CACHE
    tree = _TREES[name]
    root = cache_dir / f"{tree}-{size}-{seed}-v{GENERATOR_VERSION}"
    if (root / ".complete").exists():
        return root / "catalog"
    if root.exists():
        shutil.rmtree(root)
    build(tree, size, root / "catalog", seed)
    (root / ".complete").write_text("", encoding="utf-8")
    return root / "catalog"


def tree_digest(root: Path) -> str:
    """One sha256 over every file's relative path and bytes, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


@contextmanager
def target(name: str, size: str, seed: int = 0, cache_dir: Path | None = None) -> Iterator[Target]:
    """Build or reuse the scenario, and yield what to pass to ``rashid.validate``.

    For ``remote`` the tree is served over a local HTTPS server until the
    block ends. Every transport, the live probes included, reads
    ``rashid._store``'s certificate option, which the server sets.

    ``remote-data`` serves its tree the same way, with the data pass on and
    the live pass off, so the server sees the data pass's requests alone.
    """
    root = cached(name, size, seed, cache_dir)
    if name == "metadata":
        yield Target(root, {"data": False})
        return
    if name == "data":
        yield Target(root, {"data": True})
        return
    if name == "remote-data":
        with _remote_data(root) as found:
            yield found
        return
    from tests._https import Route, serving

    with serving() as server:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            server.routes["/" + path.relative_to(root).as_posix()] = Route(body=path.read_bytes())
        yield Target(server.url("catalog.json"), {"data": False, "live": True}, server)


@contextmanager
def _remote_data(root: Path) -> Iterator[Target]:
    """Serve the ``data`` tree from a child process, ``REMOTE_LATENCY`` on each asset.

    GDAL reads a ``/vsicurl`` URL through libcurl, which does not read
    ``rashid._store``'s certificate option. rasterio points libcurl at certifi
    through ``GDAL_CURL_CA_BUNDLE`` on import, and that option outranks
    ``CURL_CA_BUNDLE``. So the variable names the server's CA for the
    duration, and TLS verification stays on.
    """
    from tests._https import serving_tree

    with serving_tree(root, REMOTE_LATENCY) as server, tempfile.TemporaryDirectory() as scratch:
        bundle = Path(scratch) / "ca.pem"
        bundle.write_bytes(server.ca_pem)
        with mock.patch.dict(os.environ, {"GDAL_CURL_CA_BUNDLE": str(bundle)}):
            yield Target(server.url("catalog.json"), {"data": True, "live": False}, server)


def describe() -> str:
    """One line per scenario and size, for ``--help`` output."""
    lines = []
    for size in SIZES:
        collections, items = _METADATA_SHAPE[size]
        lines.append(f"metadata/{size}: {collections} collections x {items} items")
        lines.append(f"data/{size}: {_DATA_SHAPE[size]} GeoParquet + {_DATA_SHAPE[size]} COG items")
        lines.append(f"remote/{size}: metadata/{size} over local HTTPS, live pass on")
        lines.append(
            f"remote-data/{size}: data/{size} over local HTTPS, "
            f"{REMOTE_LATENCY * 1000:.0f} ms per asset request"
        )
    return "\n".join(lines)
