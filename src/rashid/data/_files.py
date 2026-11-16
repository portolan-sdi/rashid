"""Counted file opens and shared asset handles for the data checks."""

from __future__ import annotations

import contextlib
import glob as globmodule
import math
import re
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import rasterio

from rashid import _store, _trace
from rashid.data.reader import Locator


def _parquet_file(source: Any) -> Any:
    """``pq.ParquetFile(source)``, counted. Each open reads and parses the footer."""
    _trace.count("parquet.opens")
    return pq.ParquetFile(source)


def _raster_open(path: str, *, overview_level: int | None = None) -> Any:
    """``rasterio.open(path)``, counted. Each open reads the TIFF header."""
    _trace.count("raster.opens")
    options = {} if overview_level is None else {"overview_level": overview_level}
    return rasterio.open(path, **options)


class AssetFiles:
    """One asset's open Parquet file and raster dataset, shared by its checks.

    Each check used to open the asset itself, so a GeoParquet footer was read
    three times and a COG header four (#224). On a remote asset each open is
    its own round of range requests. Here the first check to ask opens the
    file and the rest reuse it.

    An open that fails is remembered too, and every later call raises the same
    exception object. Each check keeps its own ``except`` branch, so a check
    reports what it reported when it opened the file itself, down to the
    exception text in the message. The traceback is dropped before each raise,
    so it does not grow by one check's frames per raise.

    The raster dataset is opened by whoever asks first, under the GDAL
    environment active at that moment. Callers read it under
    ``rasterio.Env(GDAL_PAM_ENABLED="NO")``, since GTiff loads PAM lazily.
    """

    def __init__(self, located: Locator) -> None:
        self.located = located
        self._parquet: Any = None
        self._parquet_error: Exception | None = None
        self._handle: Any = None
        self._raster: Any = None
        self._raster_error: Exception | None = None

    def parquet(self) -> Any:
        if self._parquet_error is not None:
            raise self._parquet_error.with_traceback(None)
        if self._parquet is None:
            try:
                source: Any = self.located.source
                if self.located.is_remote:
                    source = self._handle = self.located.open_binary()
                self._parquet = _parquet_file(source)
            except Exception as exc:
                self._parquet_error = exc
                raise
        return self._parquet

    def raster(self) -> Any:
        if self._raster_error is not None:
            raise self._raster_error.with_traceback(None)
        if self._raster is None:
            try:
                self._raster = _raster_open(self.located.gdal_path())
            except Exception as exc:
                self._raster_error = exc
                raise
        return self._raster

    def overview_shapes(self) -> list[tuple[int, int]]:
        """Actual (height, width) of each level; Rasterio's factors are rounded.

        Rasterio exposes rounded width-based factors, not overview dimensions.
        Open only ambiguous levels; ordinary pyramids reuse the base dataset.
        """
        src = self.raster()
        shapes: list[tuple[int, int]] = []
        for level, factor in enumerate(src.overviews(1)):
            height, width = math.ceil(src.height / factor), math.ceil(src.width / factor)
            if factor == 1 or src.width % factor or src.height * width != src.width * height:
                # Rounding, a saturated axis, or unequal pixel scales makes the factor ambiguous.
                with _raster_open(self.located.gdal_path(), overview_level=level) as overview:
                    height, width = overview.height, overview.width
            shapes.append((height, width))
        return shapes

    def close(self) -> None:
        # One close that raises must not leave the other handles open.
        with contextlib.ExitStack() as stack:
            for opened in (self._handle, self._parquet, self._raster):
                if opened is not None:
                    stack.callback(opened.close)

    def __enter__(self) -> AssetFiles:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def partition_files(base: Path, pattern: str) -> list[tuple[str, Path]]:
    """List a locally scoped glob in stable order with POSIX message paths.

    The caller owns href applicability and containment; this owner performs
    filesystem enumeration only.
    """
    matches = sorted(globmodule.glob(pattern, root_dir=str(base), recursive=True))
    return [(Path(match).as_posix(), base / match) for match in matches]


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A regex that matches the keys a partition glob names.

    ``*`` and ``?`` stay within one path segment, ``**`` spans segments, and
    ``[...]`` is a character class (``[!...]`` negates it), as in DuckDB,
    GDAL, and fsspec.
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and (end := pattern.find("]", i + 2)) != -1:
            body = pattern[i + 1 : end]
            body = "^" + body[1:] if body.startswith("!") else body
            out.append(f"[{body.replace(chr(92), chr(92) * 2)}]")
            i = end + 1
            continue
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out))


def listing_prefix(pattern: str) -> str:
    """The directory before the glob's first wildcard: the narrowest listing that
    still returns every match."""
    literal = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    return literal.rpartition("/")[0]


def remote_partition_files(
    scheme: str, bucket: str, pattern: str, *, limit: int, config: dict[str, Any]
) -> tuple[list[tuple[str, int]], int, bool]:
    """The ``(key, size)`` of each listed object the glob matches, sorted by key.

    Also returns how many objects the listing returned, and whether it
    stopped at ``limit`` with more to come. A listing failure is a
    :class:`rashid._store.StoreError`.
    """
    listed, truncated = _store.list_objects(
        scheme, bucket, listing_prefix(pattern), limit=limit, config=config
    )
    regex = glob_regex(pattern)
    matched = sorted((key, size) for key, size in listed if regex.fullmatch(key))
    return matched, len(listed), truncated
