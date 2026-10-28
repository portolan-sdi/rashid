"""Resolve asset hrefs to their bytes — local files and remote ``https``.

The data pass needs an asset's actual bytes two ways: a whole-object stream
(checksum and size must see every byte) and seekable random access (a Parquet
footer or COG/PMTiles header is a small read at a known offset). This module
provides both over the two href kinds a conformant catalog uses: relative paths
resolved against the catalog tree, and absolute ``https`` URLs. Remote reads go
through :mod:`rashid._store`; the geospatial parsing lives in :mod:`rashid.data.checks`.

Non-fetchable hrefs — ``s3``/``http``/``file`` or a relative path that escapes
the tree — resolve to ``None``; the metadata pass already reports those
(``PTL-AST-002`` flags ``s3``), so the data pass skips them rather than emitting
a second finding.

A caller can narrow that further. :class:`LocalOnlyReader` resolves only what
lives in the tree, which is what a metadata-only mirror needs: the checks that
read its own files run, and the assets hosted elsewhere — terabytes of them —
are treated as unfetchable rather than streamed.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from rashid import _store
from rashid.catalog import CatalogGraph, Node, is_absolute_href
from rashid.remote import wire_url

_CHUNK = 1 << 16  # 64 KiB


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
    """The default reader: local paths under the catalog root, plus ``https``.

    For a catalog read over https (``graph.base_url`` set), a relative href
    names a file the crawl did not fetch — assets are not documents — so it
    resolves to its URL under the base instead of to the tree on disk.
    """

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
        if path.is_file():
            return Locator(is_remote=False, source=str(path))
        if self._graph.base_url is not None:
            return Locator(is_remote=True, source=wire_url(self._graph.base_url, str(rel)))
        return None

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        located = self.locate(node, href)
        if located is None:
            return None
        if located.is_remote:
            return _http_stream(located.source)
        return _file_stream(Path(located.source))


class LocalOnlyReader:
    """Resolves only assets inside the catalog tree; remote hrefs are ``None``.

    Delegates to :class:`FilesystemHttpReader` and drops the remote half of its
    answer, so path resolution — the part with the escaping-relative-href rules
    — has one implementation. A remote asset becomes indistinguishable from an
    ``s3`` one: unfetchable, so every check that needs its bytes is skipped
    without a network read and without a second finding.
    """

    def __init__(self, graph: CatalogGraph) -> None:
        self._inner = FilesystemHttpReader(graph)

    def locate(self, node: Node, href: str) -> Locator | None:
        located = self._inner.locate(node, href)
        if located is None or located.is_remote:
            return None
        return located

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        if self.locate(node, href) is None:
            return None
        return self._inner.stream(node, href)


def _file_stream(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            yield chunk


def _http_stream(url: str) -> Iterator[bytes]:
    return _store.stream(url, _CHUNK)


class _HttpRangeFile(io.RawIOBase):
    """A seekable, read-only file over HTTP range requests.

    Enough of the file protocol for ``pyarrow.parquet.ParquetFile`` and the
    PMTiles reader to pull a footer or header without downloading the object.
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self._pos = 0
        self._size = _store.head_size(url)

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
        end = min(self._pos + len(buffer), self._size)
        data = _store.get_range(self._url, self._pos, end)
        n = len(data)
        buffer[:n] = data
        self._pos += n
        return n
