"""Resolve asset hrefs to their bytes — local files and remote ``https``.

The data pass needs an asset's actual bytes two ways: a whole-object stream
(checksum and size must see every byte) and seekable random access (a Parquet
footer or COG/PMTiles header is a small read at a known offset). This module
provides both over the two href kinds a conformant catalog uses: relative paths
resolved against the catalog tree, and absolute ``https`` URLs. It is
stdlib-only (``urllib``); the geospatial parsing lives in :mod:`rashid.data.checks`.

Non-fetchable hrefs — ``s3``/``http``/``file`` or a relative path that escapes
the tree — resolve to ``None``; the metadata pass already reports those
(``PTL-AST-002`` flags ``s3``), so the data pass skips them rather than emitting
a second finding.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rashid._http import user_agent
from rashid.catalog import CatalogGraph, Node, is_absolute_href

_CHUNK = 1 << 16  # 64 KiB
_TIMEOUT = 30  # seconds per request


def _headers(**extra: str) -> dict[str, str]:
    """Request headers, always naming the validator.

    urllib's default agent is blocked by edge providers, which answer it with
    an error page the reader would otherwise consume as the asset's bytes.
    """
    return {"User-Agent": user_agent(), **extra}


@dataclass(frozen=True)
class Locator:
    """Where an asset's bytes live: a local path string, or an ``https`` URL."""

    is_remote: bool
    source: str

    def gdal_path(self) -> str:
        """The path GDAL/rasterio opens: a local path, or a ``/vsicurl/`` URL."""
        return f"/vsicurl/{self.source}" if self.is_remote else self.source

    def open_binary(self) -> io.IOBase:
        """A seekable binary handle: a local file, or an HTTP-range reader."""
        if self.is_remote:
            return _HttpRangeFile(self.source)
        return Path(self.source).open("rb")


class AssetReader(Protocol):
    """Resolves and reads the bytes an asset href points at."""

    def locate(self, node: Node, href: str) -> Locator | None:
        """Where the asset's bytes are, or ``None`` when not fetchable."""

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        """Yield the whole object in chunks, or ``None`` when not fetchable."""


class FilesystemHttpReader:
    """The default reader: local paths under the catalog root, plus ``https``."""

    def __init__(self, graph: CatalogGraph) -> None:
        self._graph = graph

    def locate(self, node: Node, href: str) -> Locator | None:
        if not href:
            return None
        if is_absolute_href(href):
            if urlparse(href).scheme.lower() == "https":
                return Locator(is_remote=True, source=href)
            return None  # s3/http/file: not browser-fetchable; PTL-AST-002 covers it
        rel = self._graph.resolve_path(node, href)
        if rel is None:
            return None
        path = self._graph.root_path / Path(*rel.parts)
        if not path.is_file():
            return None
        return Locator(is_remote=False, source=str(path))

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        located = self.locate(node, href)
        if located is None:
            return None
        if located.is_remote:
            return _http_stream(located.source)
        return _file_stream(Path(located.source))


def _file_stream(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _http_stream(url: str) -> Iterator[bytes]:
    _require_https(url)
    request = Request(url, method="GET", headers=_headers())
    with urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310  # nosec B310
        while chunk := response.read(_CHUNK):
            yield chunk


def _require_https(url: str) -> None:
    if urlparse(url).scheme.lower() != "https":
        raise ValueError(f"refusing to fetch non-https URL: {url!r}")


class _HttpRangeFile(io.RawIOBase):
    """A seekable, read-only file over HTTP range requests.

    Enough of the file protocol for ``pyarrow.parquet.ParquetFile`` and the
    PMTiles reader to pull a footer or header without downloading the object.
    Servers that ignore ``Range`` and return the whole body (200) are handled by
    slicing; the spec requires range support, so this is a fallback, not the path.
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        _require_https(url)
        self._url = url
        self._pos = 0
        self._size = self._head_length()

    def _head_length(self) -> int:
        request = Request(self._url, method="HEAD", headers=_headers())
        with urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310  # nosec B310
            length = response.headers.get("Content-Length")
        if length is None:
            raise OSError(f"HEAD {self._url!r} returned no Content-Length")
        return int(length)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        else:  # pragma: no cover - io guarantees one of the three
            raise ValueError(f"invalid whence: {whence}")
        return self._pos

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        if self._pos >= self._size:
            return 0
        want = len(buffer)
        end = min(self._pos + want, self._size) - 1  # inclusive
        request = Request(
            self._url,
            method="GET",
            headers=_headers(Range=f"bytes={self._pos}-{end}"),
        )
        with urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310  # nosec B310
            data = response.read()
            if response.status == 200:  # server ignored Range; slice ourselves
                data = data[self._pos : end + 1]
        n = len(data)
        buffer[:n] = data
        self._pos += n
        return n
