"""Counted file opens and shared asset handles for the data checks."""

from __future__ import annotations

import contextlib
import math
from typing import Any

import pyarrow.parquet as pq
import rasterio

from rashid import _trace
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


class _AssetFiles:
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
            if factor == 1 or src.height * width != src.width * height:
                # A saturated axis or unequal pixel scales makes the factor ambiguous.
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

    def __enter__(self) -> _AssetFiles:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class _FileCache:
    """Node-scoped handles, including partitions also declared as assets."""

    def __init__(self) -> None:
        self._opened: dict[Locator, _AssetFiles] = {}
        self._stack = contextlib.ExitStack()

    def get(self, located: Locator) -> _AssetFiles:
        if located not in self._opened:
            self._opened[located] = self._stack.enter_context(_AssetFiles(located))
        return self._opened[located]

    def __enter__(self) -> _FileCache:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stack.close()
