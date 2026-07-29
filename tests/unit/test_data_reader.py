"""Unit tests for the asset reader — local resolution and stubbed HTTP.

Stdlib-only: the real ``urlopen`` is monkeypatched, so these need no network and
no ``rashid[data]`` extra. They cover href classification, local streaming, and the
HTTP range/stream fallbacks the remote path relies on.
"""

from __future__ import annotations

import io
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest

import rashid.data.reader as reader_mod
from rashid.catalog import CatalogGraph, Node
from rashid.data.reader import FilesystemHttpReader, Locator, _HttpRangeFile
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


# --- stubbed HTTP ----------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes, status: int, headers: dict[str, str]) -> None:
        self._body = body
        self._pos = 0
        self.status = status
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._body[self._pos :]
        else:
            chunk = self._body[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_server(payload: bytes, *, honor_range: bool = True) -> Any:
    def fake_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
        if request.get_method() == "HEAD":
            return _FakeResponse(b"", 200, {"Content-Length": str(len(payload))})
        rng = request.get_header("Range")
        if rng and honor_range:
            start_s, end_s = rng.removeprefix("bytes=").split("-")
            start, end = int(start_s), int(end_s)
            return _FakeResponse(payload[start : end + 1], 206, {})
        return _FakeResponse(payload, 200, {})

    return fake_urlopen


def test_http_stream_reads_whole_object(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, item = _item_graph(catalog)
    payload = bytes(range(256)) * 500
    monkeypatch.setattr(reader_mod, "urlopen", _fake_server(payload))

    stream = FilesystemHttpReader(graph).stream(item, "https://host/x.parquet")
    assert stream is not None
    assert b"".join(stream) == payload


def test_range_file_seek_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = bytes(range(256))
    monkeypatch.setattr(reader_mod, "urlopen", _fake_server(payload))

    handle = _HttpRangeFile("https://host/x.bin")
    assert handle.seekable() and handle.readable()

    handle.seek(-4, io.SEEK_END)
    assert handle.tell() == 252
    assert handle.read(4) == payload[252:256]

    handle.seek(10)
    assert handle.read(5) == payload[10:15]
    assert handle.tell() == 15


def test_range_file_past_end_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_mod, "urlopen", _fake_server(b"abcd"))
    handle = _HttpRangeFile("https://host/x.bin")
    handle.seek(10)
    assert handle.read(4) == b""


def test_range_file_slices_when_server_ignores_range(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = bytes(range(64))
    monkeypatch.setattr(reader_mod, "urlopen", _fake_server(payload, honor_range=False))

    handle = _HttpRangeFile("https://host/x.bin")
    handle.seek(8)
    assert handle.read(4) == payload[8:12]


def test_range_file_requires_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_length(request: Any, **_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(b"", 200, {})

    monkeypatch.setattr(reader_mod, "urlopen", no_length)
    with pytest.raises(OSError, match="Content-Length"):
        _HttpRangeFile("https://host/x.bin")


def test_non_https_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="non-https"):
        _HttpRangeFile("http://host/x.bin")


def test_local_open_binary_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "a.bin"
    path.write_bytes(b"hello")
    located = Locator(is_remote=False, source=str(path))
    with located.open_binary() as handle:
        assert handle.read() == b"hello"


# --- outgoing request headers ------------------------------------------------
#
# The stubs above answer any request, so a missing User-Agent is invisible to
# them. These record the Request each read path builds and assert on it (#64).

# urllib capitalizes header names, so the stored key is 'User-agent'.
_AGENT_PATTERN = re.compile(r"^rashid/\S+ \(\+https://github\.com/portolan-sdi/rashid\)$")


def _recorder(requests: list[Request], payload: bytes) -> Any:
    server = _fake_server(payload)

    def fake_urlopen(request: Request, **kwargs: Any) -> _FakeResponse:
        requests.append(request)
        return server(request, **kwargs)

    return fake_urlopen


def _assert_named_agent(request: Request) -> None:
    agent = request.get_header("User-agent")
    assert agent is not None, f"{request.get_method()} sent no User-Agent"
    assert _AGENT_PATTERN.match(agent), agent
    assert agent == f"rashid/{version('rashid')} (+https://github.com/portolan-sdi/rashid)"


def test_http_stream_sends_a_named_user_agent(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, item = _item_graph(catalog)
    requests: list[Request] = []
    monkeypatch.setattr(reader_mod, "urlopen", _recorder(requests, b"payload"))

    stream = FilesystemHttpReader(graph).stream(item, "https://host/x.parquet")
    assert stream is not None
    b"".join(stream)

    (request,) = requests
    assert request.get_method() == "GET"
    _assert_named_agent(request)


def test_range_file_head_sends_a_named_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []
    monkeypatch.setattr(reader_mod, "urlopen", _recorder(requests, bytes(range(64))))

    _HttpRangeFile("https://host/x.bin")

    (request,) = requests
    assert request.get_method() == "HEAD"
    _assert_named_agent(request)


def test_range_read_sends_a_named_user_agent_and_keeps_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = bytes(range(64))
    requests: list[Request] = []
    monkeypatch.setattr(reader_mod, "urlopen", _recorder(requests, payload))

    handle = _HttpRangeFile("https://host/x.bin")
    handle.seek(8)
    assert handle.read(4) == payload[8:12]

    ranged = requests[-1]
    assert ranged.get_method() == "GET"
    assert ranged.get_header("Range") == "bytes=8-11"
    _assert_named_agent(ranged)
