"""Unit tests for rashid._store: obstore reads against a local HTTPS server."""

from __future__ import annotations

import socket
from datetime import timedelta
from importlib.metadata import version
from typing import Any

import pytest

from rashid import _store, _trace
from tests._https import HttpsServer, Route

_AGENT = f"rashid/{version('rashid')} (+https://github.com/portolan-sdi/rashid)"


def test_get_returns_the_body_and_sends_the_named_agent(https_server: HttpsServer) -> None:
    https_server.routes["/a/doc.json"] = Route(body=b'{"a": 1}')

    assert _store.get(https_server.url("a/doc.json")) == b'{"a": 1}'
    (seen,) = https_server.seen
    assert (seen.method, seen.path) == ("GET", "/a/doc.json")
    assert seen.headers["user-agent"] == _AGENT


@pytest.mark.parametrize("status", [403, 404, 405, 410, 500])
def test_get_reports_the_status(https_server: HttpsServer, status: int) -> None:
    https_server.routes["/x.json"] = Route(status=status)

    with pytest.raises(_store.StoreError) as raised:
        _store.get(https_server.url("x.json"))
    assert raised.value.status == status
    assert f"returned {status}" in str(raised.value)


def test_a_failure_is_an_os_error(https_server: HttpsServer) -> None:
    with pytest.raises(OSError):
        _store.get(https_server.url("missing.json"))


def test_get_without_a_server_has_no_status() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(_store.StoreError) as raised:
        _store.get(f"https://127.0.0.1:{port}/catalog.json")
    assert raised.value.status is None
    assert not raised.value.refused


def test_get_refuses_a_body_past_the_cap(https_server: HttpsServer) -> None:
    https_server.routes["/big.json"] = Route(body=b"x" * 100)

    with pytest.raises(_store.StoreError, match="exceeds 99 bytes"):
        _store.get(https_server.url("big.json"), max_bytes=99)
    assert _store.get(https_server.url("big.json"), max_bytes=100) == b"x" * 100


def test_get_follows_a_redirect_to_https(https_server: HttpsServer) -> None:
    https_server.routes["/old.json"] = Route(status=301, headers={"Location": "/new.json"})
    https_server.routes["/new.json"] = Route(body=b"{}")

    assert _store.get(https_server.url("old.json")) == b"{}"


def test_get_refuses_a_redirect_to_http(https_server: HttpsServer) -> None:
    https_server.routes["/old.json"] = Route(
        status=302, headers={"Location": "http://127.0.0.1:9/new.json"}
    )

    with pytest.raises(_store.StoreError) as raised:
        _store.get(https_server.url("old.json"))
    assert raised.value.status is None
    assert raised.value.refused
    assert "redirect" in str(raised.value)
    assert len(https_server.seen) == 1  # urllib would refuse it too; not asked again


def test_a_body_without_content_length_is_read_over_urllib(https_server: HttpsServer) -> None:
    """obstore refuses the answer; urllib reads it, with the same agent."""
    https_server.routes["/chunked.json"] = Route(body=b"{}", length=False)

    assert _store.get(https_server.url("chunked.json")) == b"{}"
    assert b"".join(_store.stream(https_server.url("chunked.json"), 1 << 16)) == b"{}"
    assert [s.method for s in https_server.seen] == ["GET"] * 4
    assert {s.headers["user-agent"] for s in https_server.seen} == {_AGENT}


def test_urllib_keeps_the_body_cap(https_server: HttpsServer) -> None:
    https_server.routes["/chunked.json"] = Route(body=b"x" * 100, length=False)

    with pytest.raises(_store.StoreError, match="exceeds 99 bytes"):
        _store.get(https_server.url("chunked.json"), max_bytes=99)


def test_a_head_without_content_length_is_refused(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin"] = Route(body=b"abc", length=False)

    with pytest.raises(_store.StoreError) as raised:
        _store.head_size(https_server.url("x.bin"))
    assert raised.value.refused
    assert raised.value.status is None
    assert len(https_server.seen) == 1  # urllib has no size to read either


def test_the_path_is_encoded_once(https_server: HttpsServer) -> None:
    https_server.routes["/a%20b/c%25d.json"] = Route(body=b"{}")

    assert _store.get(https_server.url("a%20b/c%25d.json")) == b"{}"
    assert https_server.seen[0].path == "/a%20b/c%25d.json"


def test_a_semicolon_stays_in_the_path(https_server: HttpsServer) -> None:
    https_server.routes["/a;v=1.json"] = Route(body=b"{}")

    assert _store.get(https_server.url("a;v=1.json")) == b"{}"
    assert https_server.seen[0].path == "/a;v=1.json"


@pytest.mark.parametrize("url", ["http://example.org/x.json", "s3://bucket/x.json"])
def test_non_https_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="non-https"):
        _store.get(url)


@pytest.mark.parametrize(
    "path",
    ["/x.json?v=1", "/a%2Fb.json", "/a//b.json", "/a/./b.json", "/a/../b.json", "/"],
    ids=["query", "encoded-slash", "empty-segment", "dot", "dot-dot", "host-root"],
)
def test_urls_obstore_cannot_request_go_over_urllib_as_written(
    https_server: HttpsServer, path: str
) -> None:
    https_server.routes[path] = Route(body=b"0123456789")
    url = https_server.base + path

    assert _store.get(url) == b"0123456789"
    assert _store.head_size(url) == 10
    assert _store.get_range(url, 2, 5) == b"234"
    assert b"".join(_store.stream(url, 4)) == b"0123456789"
    assert [s.path for s in https_server.seen] == [path] * 4
    assert https_server.seen[2].headers["range"] == "bytes=2-4"


def test_urllib_reports_the_status(https_server: HttpsServer) -> None:
    with pytest.raises(_store.StoreError) as raised:
        _store.get(https_server.url("missing.json?v=1"))
    assert raised.value.status == 404
    assert "returned 404" in str(raised.value)


def test_urllib_slices_when_the_server_ignores_range(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin?v=1"] = Route(body=bytes(range(64)), honor_range=False)

    assert _store.get_range(https_server.url("x.bin?v=1"), 8, 12) == bytes(range(8, 12))


def test_urllib_refuses_a_redirect_to_http(https_server: HttpsServer) -> None:
    https_server.routes["/old.json?v=1"] = Route(
        status=302, headers={"Location": "http://127.0.0.1:9/new.json"}
    )

    with pytest.raises(_store.StoreError) as raised:
        _store.get(https_server.url("old.json?v=1"))
    assert raised.value.refused
    assert len(https_server.seen) == 1


def test_head_size_reads_content_length(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin"] = Route(body=b"abcdef")

    assert _store.head_size(https_server.url("x.bin")) == 6
    assert [s.method for s in https_server.seen] == ["HEAD"]
    assert https_server.seen[0].headers["user-agent"] == _AGENT


def test_head_size_reports_the_status(https_server: HttpsServer) -> None:
    with pytest.raises(_store.StoreError) as raised:
        _store.head_size(https_server.url("missing.bin"))
    assert raised.value.status == 404


def test_get_range_reads_a_slice(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin"] = Route(body=bytes(range(64)))

    assert _store.get_range(https_server.url("x.bin"), 8, 12) == bytes(range(8, 12))
    assert https_server.seen[0].headers["range"] == "bytes=8-11"


def test_get_range_slices_when_the_server_ignores_range(https_server: HttpsServer) -> None:
    https_server.routes["/x.bin"] = Route(body=bytes(range(64)), honor_range=False)

    assert _store.get_range(https_server.url("x.bin"), 8, 12) == bytes(range(8, 12))


def test_stream_yields_the_whole_body_lazily(https_server: HttpsServer) -> None:
    payload = bytes(range(256)) * 1000
    https_server.routes["/x.bin"] = Route(body=payload)

    chunks = _store.stream(https_server.url("x.bin"), 1 << 16)
    assert https_server.seen == []  # nothing sent before iteration
    assert b"".join(chunks) == payload


def test_stream_failure_is_an_os_error(https_server: HttpsServer) -> None:
    with pytest.raises(OSError, match="returned 404"):
        b"".join(_store.stream(https_server.url("missing.bin"), 1 << 16))


def test_a_download_may_outlast_the_read_timeout(
    https_server: HttpsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout bounds each wait for bytes, not the whole download.

    A large asset takes longer than any single wait to hash. A limit on the
    whole request would turn every such asset into an unread one.
    """
    monkeypatch.setitem(_store.CLIENT_OPTIONS, "read_timeout", timedelta(seconds=0.5))
    _store.reset()
    https_server.routes["/x.bin"] = Route(body=b"x" * 8, delay=0.2)  # 1.4 s in all

    assert b"".join(_store.stream(https_server.url("x.bin"), 1)) == b"x" * 8


def test_a_stalled_download_fails(
    https_server: HttpsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(_store.CLIENT_OPTIONS, "read_timeout", timedelta(seconds=0.3))
    monkeypatch.setitem(_store._RETRY, "max_retries", 0)
    _store.reset()
    https_server.routes["/x.bin"] = Route(body=b"xx", delay=3)

    with pytest.raises(_store.StoreError) as raised:
        b"".join(_store.stream(https_server.url("x.bin"), 1))
    assert raised.value.status is None
    assert not raised.value.refused


def test_the_overall_timeout_is_only_a_backstop() -> None:
    assert _store.CLIENT_OPTIONS["read_timeout"] == timedelta(seconds=30)
    assert _store.CLIENT_OPTIONS["timeout"] >= timedelta(hours=1)


def test_one_store_per_host(https_server: HttpsServer) -> None:
    https_server.routes["/a.json"] = Route(body=b"{}")
    https_server.routes["/b/c.json"] = Route(body=b"{}")

    _store.get(https_server.url("a.json"))
    _store.get(https_server.url("b/c.json"))
    assert _store._store.cache_info().currsize == 1


def test_only_the_store_and_the_live_prober_import_urllib_request() -> None:
    """Every read goes through this module, except the live pass's per-host probes.

    Those probes read CORS and range headers, and send OPTIONS, which obstore
    does not support. This module sends over urllib what obstore cannot. Any
    other module that reaches for urllib.request bypasses the connection pool
    and the shared error handling here.
    """
    import ast
    from pathlib import Path

    import rashid

    importers: set[str] = set()
    for path in sorted(Path(rashid.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name.startswith("urllib.request") for name in names):
                importers.add(path.name)
    assert importers == {"_store.py", "live.py"}


def test_urllib_follows_a_redirect_to_https(https_server: HttpsServer) -> None:
    https_server.routes["/old.json?v=1"] = Route(status=301, headers={"Location": "/new.json"})
    https_server.routes["/new.json"] = Route(body=b"{}")

    assert _store.get(https_server.url("old.json?v=1")) == b"{}"


def test_urllib_without_a_server_has_no_status() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(_store.StoreError) as raised:
        _store.get(f"https://127.0.0.1:{port}/catalog.json?v=1")
    assert raised.value.status is None
    assert not raised.value.refused


def test_get_range_reports_the_status(https_server: HttpsServer) -> None:
    with pytest.raises(_store.StoreError) as raised:
        _store.get_range(https_server.url("missing.bin"), 0, 4)
    assert raised.value.status == 404


@pytest.mark.parametrize("path", ["/x.json", "/x.json?v=1"], ids=["obstore", "urllib"])
def test_both_transports_retry_a_5xx(https_server: HttpsServer, path: str) -> None:
    https_server.routes[path] = Route(body=b"{}", fail_first=2)

    assert _store.get(https_server.base + path) == b"{}"
    assert len(https_server.seen) == 3


@pytest.mark.parametrize("path", ["/x.json", "/x.json?v=1"], ids=["obstore", "urllib"])
def test_both_transports_give_up_after_the_retries(
    https_server: HttpsServer, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setitem(_store._RETRY, "max_retries", 2)
    _store.reset()
    https_server.routes[path] = Route(body=b"{}", fail_first=10)

    with pytest.raises(_store.StoreError) as raised:
        _store.get(https_server.base + path)
    assert raised.value.status == 503
    assert len(https_server.seen) == 3


def test_urllib_does_not_retry_a_4xx(https_server: HttpsServer) -> None:
    with pytest.raises(_store.StoreError):
        _store.get(https_server.url("missing.json?v=1"))
    assert len(https_server.seen) == 1


def test_urllib_retries_a_failed_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    calls: list[str] = []
    real = _store._opener

    class Counting:
        def open(self, request: Any, timeout: float) -> Any:
            calls.append(request.full_url)
            return real().open(request, timeout=timeout)

    monkeypatch.setattr(_store, "_opener", lambda: Counting())

    with pytest.raises(_store.StoreError) as raised:
        _store.get(f"https://127.0.0.1:{port}/x.json?v=1")
    assert raised.value.status is None
    assert len(calls) == 1 + _store._RETRY["max_retries"]


def test_counters_record_each_request_and_each_urllib_fallback(
    https_server: HttpsServer,
) -> None:
    https_server.routes["/x.bin"] = Route(body=bytes(range(64)))
    https_server.routes["/chunked.bin"] = Route(body=bytes(range(64)), length=False)
    x, chunked = https_server.url("x.bin"), https_server.url("chunked.bin")

    with _trace.counting() as delta:
        _store.get(x)
        _store.head_size(x)
        _store.get_range(x, 0, 4)
        b"".join(_store.stream(x, 16))
    assert delta == {"http.gets": 1, "http.heads": 1, "http.ranges": 1, "http.streams": 1}

    with _trace.counting() as delta:
        assert _store.get_range(chunked, 8, 12) == bytes(range(8, 12))
    assert delta == {"http.ranges": 1, "http.urllib_fallbacks": 1}


def test_counters_record_urllib_retries(https_server: HttpsServer) -> None:
    https_server.routes["/x.json?v=1"] = Route(body=b"{}", fail_first=2)

    with _trace.counting() as delta:
        assert _store.get(https_server.url("x.json?v=1")) == b"{}"
    assert delta == {"http.gets": 1, "http.urllib_fallbacks": 1, "http.retries": 2}
