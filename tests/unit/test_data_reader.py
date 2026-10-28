"""Unit tests for the asset reader — local resolution and HTTPS reads.

Remote reads go to a local HTTPS server (``tests._https``), so these need no
network. They cover href classification, local streaming, and the HTTP
range/stream fallbacks the remote path relies on.
"""

from __future__ import annotations

import io
from importlib.metadata import version
from pathlib import Path

import pytest

from rashid.catalog import CatalogGraph, Node
from rashid.data.reader import FilesystemHttpReader, Locator, _HttpRangeFile
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit


def _item_graph(catalog: CatalogBuilder) -> tuple[CatalogGraph, Node]:
    catalog.collection("roads").item("seg1")
    graph = CatalogGraph.load(catalog.write())
    item = next(node for node in graph.iter("item"))
    return graph, item


def test_locate_local_existing_file(catalog: CatalogBuilder) -> None:
    graph, item = _item_graph(catalog)
    asset = graph.root_path / "roads" / "seg1" / "data.parquet"
    asset.write_bytes(b"PAR1payload")

    located = FilesystemHttpReader(graph).locate(item, "./data.parquet")

    assert located == Locator(is_remote=False, source=str(asset))
    assert located.gdal_path() == str(asset)


def test_locate_local_missing_file_is_none(catalog: CatalogBuilder) -> None:
    graph, item = _item_graph(catalog)
    assert FilesystemHttpReader(graph).locate(item, "./gone.parquet") is None


def test_locate_https_is_remote(catalog: CatalogBuilder) -> None:
    graph, item = _item_graph(catalog)
    url = "https://data.example.org/x.parquet"

    located = FilesystemHttpReader(graph).locate(item, url)

    assert located == Locator(is_remote=True, source=url)
    assert located.gdal_path() == f"/vsicurl/{url}"


@pytest.mark.parametrize(
    "href", ["s3://bucket/x.parquet", "http://insecure/x.parquet", "", "../escape.parquet"]
)
def test_locate_unfetchable_is_none(catalog: CatalogBuilder, href: str) -> None:
    graph, item = _item_graph(catalog)
    assert FilesystemHttpReader(graph).locate(item, href) is None


def test_stream_local_yields_all_bytes(catalog: CatalogBuilder) -> None:
    graph, item = _item_graph(catalog)
    payload = b"x" * (200_000)  # spans several 64 KiB chunks
    (graph.root_path / "roads" / "seg1" / "data.parquet").write_bytes(payload)

    stream = FilesystemHttpReader(graph).stream(item, "./data.parquet")
    assert stream is not None
    assert b"".join(stream) == payload


def test_stream_missing_is_none(catalog: CatalogBuilder) -> None:
    graph, item = _item_graph(catalog)
    assert FilesystemHttpReader(graph).stream(item, "./gone.parquet") is None


# --- remote reads against a local HTTPS server -----------------------------

_AGENT = f"rashid/{version('rashid')} (+https://github.com/portolan-sdi/rashid)"


def test_http_stream_reads_whole_object(catalog: CatalogBuilder, https_server: HttpsServer) -> None:
    graph, item = _item_graph(catalog)
    payload = bytes(range(256)) * 500
    https_server.routes["/x.parquet"] = Route(body=payload)

    stream = FilesystemHttpReader(graph).stream(item, https_server.url("x.parquet"))
    assert stream is not None
    assert b"".join(stream) == payload
    (seen,) = https_server.seen
    assert seen.method == "GET" and seen.headers["user-agent"] == _AGENT


def test_http_stream_failure_is_an_os_error(
    catalog: CatalogBuilder, https_server: HttpsServer
) -> None:
    """The data pass reports an OSError from a stream as an unread asset."""
    graph, item = _item_graph(catalog)

    stream = FilesystemHttpReader(graph).stream(item, https_server.url("gone.parquet"))
    assert stream is not None
    with pytest.raises(OSError, match="returned 404"):
        b"".join(stream)


def test_range_file_seek_and_read(https_server: HttpsServer) -> None:
    payload = bytes(range(256))
    https_server.routes["/x.bin"] = Route(body=payload)

    handle = _HttpRangeFile(https_server.url("x.bin"))
    assert handle.seekable() and handle.readable()

    handle.seek(-4, io.SEEK_END)
    assert handle.tell() == 252
    assert handle.read(4) == payload[252:256]

    handle.seek(10)
    assert handle.read(5) == payload[10:15]
    assert handle.tell() == 15


def test_range_file_past_end_returns_empty(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin"] = Route(body=b"abcd")
    handle = _HttpRangeFile(https_server.url("x.bin"))
    handle.seek(10)
    assert handle.read(4) == b""


def test_range_file_slices_when_server_ignores_range(https_server: HttpsServer) -> None:
    payload = bytes(range(64))
    https_server.routes["/x.bin"] = Route(body=payload, honor_range=False)

    handle = _HttpRangeFile(https_server.url("x.bin"))
    handle.seek(8)
    assert handle.read(4) == payload[8:12]


def test_range_file_requires_content_length(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin"] = Route(body=b"abcd", length=False)
    with pytest.raises(OSError, match="Content-Length"):
        _HttpRangeFile(https_server.url("x.bin"))


def test_non_https_refused() -> None:
    with pytest.raises(ValueError, match="non-https"):
        _HttpRangeFile("http://host/x.bin")


def test_local_open_binary_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "a.bin"
    path.write_bytes(b"hello")
    located = Locator(is_remote=False, source=str(path))
    with located.open_binary() as handle:
        assert handle.read() == b"hello"


def test_range_reads_send_a_named_user_agent_and_keep_range(https_server: HttpsServer) -> None:
    payload = bytes(range(64))
    https_server.routes["/x.bin"] = Route(body=payload)

    handle = _HttpRangeFile(https_server.url("x.bin"))
    handle.seek(8)
    assert handle.read(4) == payload[8:12]

    head, ranged = https_server.seen
    assert head.method == "HEAD" and head.headers["user-agent"] == _AGENT
    assert ranged.method == "GET" and ranged.headers["user-agent"] == _AGENT
    assert ranged.headers["range"] == "bytes=8-11"
