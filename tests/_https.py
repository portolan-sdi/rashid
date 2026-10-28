"""A local HTTPS server for the tests that drive obstore end to end.

obstore sends its requests from Rust, so a monkeypatched ``urlopen`` cannot
stand in for the network. This server answers over real TLS with a
certificate from ``trustme``, and ``rashid._store`` is told to accept it. Each
test sets the routes it needs and reads back the requests the server saw.
"""

from __future__ import annotations

import json
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import trustme

from rashid import _store


@dataclass
class Route:
    """One canned answer. ``length=False`` sends no ``Content-Length``.

    ``delay`` is a pause, in seconds, before each byte of the body after the
    first, so a test can serve a slow body or one that stalls. ``latency`` is
    a pause before the status line, as a distant host takes to answer. The
    first ``fail_first`` requests answer 503 instead, as a throttling host
    does. With ``fail_agent`` set, only requests whose ``User-Agent``
    contains it count toward ``fail_first``.
    """

    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    honor_range: bool = True
    length: bool = True
    delay: float = 0.0
    latency: float = 0.0
    fail_first: int = 0
    fail_agent: str = ""


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]


class HttpsServer:
    """The routes to answer, and what the server saw.

    ``peak_in_flight`` is the most requests the server held open at once,
    which shows whether a client sent them in parallel. Only the paths that
    ``counted`` accepts enter it. ``ca_pem`` is the
    certificate authority that signed the server's certificate, for a
    client such as GDAL's libcurl that reads its trust store from a file.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.seen: list[Seen] = []
        self.base = ""
        self.ca_pem = b""
        self.in_flight = 0
        self.peak_in_flight = 0
        self.counted: Callable[[str], bool] = lambda _path: True
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def leave(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def take_failure(self, route: Route, agent: str) -> bool:
        """Whether this request answers 503, which uses up one of ``fail_first``."""
        with self._lock:
            if route.fail_first and route.fail_agent in agent:
                route.fail_first -= 1
                return True
            return False

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"


def _handler(server: HttpsServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            return None

        def _answer(self, send_body: bool) -> None:
            if not server.counted(self.path):
                self._respond(send_body)
                return
            server.enter()
            try:
                self._respond(send_body)
            finally:
                server.leave()

        def _respond(self, send_body: bool) -> None:
            server.seen.append(
                Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
            )
            route = server.routes.get(self.path, Route(status=404))
            if route.latency:
                time.sleep(route.latency)
            body, status = route.body, route.status
            if server.take_failure(route, self.headers.get("User-Agent", "")):
                body, status = b"", 503
            headers = dict(route.headers)
            byte_range = self.headers.get("Range")
            if byte_range and route.honor_range and status == 200:
                first, last = byte_range.removeprefix("bytes=").split("-")
                start, end = int(first), min(int(last), len(body) - 1)
                headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                body, status = body[start : end + 1], 206
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            if route.length:
                self.send_header("Content-Length", str(len(body)))
            else:
                self.send_header("Connection", "close")
            self.end_headers()
            if not send_body:
                return
            if not route.delay:
                self.wfile.write(body)
                return
            for index in range(len(body)):
                if index:
                    time.sleep(route.delay)
                self.wfile.write(body[index : index + 1])
                self.wfile.flush()

        def do_GET(self) -> None:
            self._answer(True)

        def do_HEAD(self) -> None:
            self._answer(False)

        def do_OPTIONS(self) -> None:
            self._answer(False)

    return Handler


class _Server(ThreadingHTTPServer):
    """A threading server that closes without waiting on idle connections.

    GDAL's libcurl keeps its connections open between requests, so a handler
    thread waits on the next request line until the client goes away. Joining
    those threads at close would hang. Each thread is a daemon, so the
    process does not wait on them either. A client that hangs up mid-answer,
    as a stream closed after its first bytes does, is not an error here.
    """

    block_on_close = False

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


@contextmanager
def serving() -> Iterator[HttpsServer]:
    """Run a local HTTPS server that ``rashid._store`` trusts until the block ends.

    ``scripts/profile.py`` and ``tests/scenarios.py`` use this outside pytest,
    so it sets and restores ``allow_invalid_certificates`` itself.
    """
    authority = trustme.CA()
    certificate = authority.issue_cert("127.0.0.1", "localhost")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    certificate.configure_cert(context)
    state = HttpsServer()
    state.ca_pem = authority.cert_pem.bytes()
    httpd = _Server(("127.0.0.1", 0), _handler(state))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    state.base = f"https://127.0.0.1:{httpd.server_address[1]}"
    # A short poll interval: shutdown() waits up to one interval, once per test.
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    previous = _store.CLIENT_OPTIONS.get("allow_invalid_certificates", _UNSET)
    _store.CLIENT_OPTIONS["allow_invalid_certificates"] = True
    _store.reset()
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        if previous is _UNSET:
            del _store.CLIENT_OPTIONS["allow_invalid_certificates"]
        else:
            _store.CLIENT_OPTIONS["allow_invalid_certificates"] = previous
        _store.reset()


_UNSET = object()


@pytest.fixture
def https_server() -> Iterator[HttpsServer]:
    """A running local HTTPS server that ``rashid._store`` trusts."""
    with serving() as state:
        yield state


# --- a server in its own process ----------------------------------------------

#: The files a crawl fetches. Every other file in a served tree is an asset.
DOCUMENT_SUFFIXES = (".json", ".md")


class ServedTree:
    """A directory served over HTTPS by a child process, and what it saw.

    rasterio calls into GDAL while it holds the GIL, and GDAL can send a
    request from inside such a call. A server in the same process then cannot
    answer, and the run deadlocks rather than fails. With its own process and
    its own GIL, the server answers whatever the client does, so a test that
    counts requests fails on a regression instead of hanging on one.
    """

    def __init__(self, child: subprocess.Popen[str], base: str, ca_pem: bytes) -> None:
        self._child = child
        self.base = base
        self.ca_pem = ca_pem

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def _ask(self, command: str) -> dict[str, Any]:
        assert self._child.stdin is not None and self._child.stdout is not None
        self._child.stdin.write(command + "\n")
        self._child.stdin.flush()
        reply: dict[str, Any] = json.loads(self._child.stdout.readline())
        return reply

    def seen(self) -> list[Seen]:
        """Every request since the start or the last :meth:`reset`."""
        return [Seen(method, path, headers) for method, path, headers in self._ask("seen")["seen"]]

    def peak_in_flight(self) -> int:
        """The most asset requests open at once. Documents do not count."""
        return int(self._ask("peak")["peak"])

    def fail(self, path: str, count: int, agent: str) -> None:
        """Answer 503 to the next ``count`` requests for ``path`` from ``agent``."""
        self._ask(f"fail {path} {count} {agent}")

    def reset(self) -> None:
        self._ask("reset")


@contextmanager
def serving_tree(root: Path, latency: float = 0.0) -> Iterator[ServedTree]:
    """Serve every file under ``root`` from a child process until the block ends.

    Each file is a route at its path relative to ``root``. Every file but a
    ``.json`` document waits ``latency`` seconds before its answer, so the
    crawl stays fast and the asset reads carry the cost. The peak in flight
    counts the asset requests only. The crawl fetches each level's documents
    and its ``.md`` files on many threads, and its overlap must not pass for
    the data pass's. ``rashid._store``
    trusts the child's certificate for the duration, as with :func:`serving`.
    """
    child = subprocess.Popen(
        [sys.executable, "-m", "tests._https", str(root), str(latency)],
        cwd=Path(__file__).resolve().parent.parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    ready = json.loads(child.stdout.readline())
    previous = _store.CLIENT_OPTIONS.get("allow_invalid_certificates", _UNSET)
    _store.CLIENT_OPTIONS["allow_invalid_certificates"] = True
    _store.reset()
    try:
        yield ServedTree(child, ready["base"], ready["ca_pem"].encode())
    finally:
        # Restore the certificate option first: a child that will not stop
        # must not leave verification off for the rest of the session.
        if previous is _UNSET:
            del _store.CLIENT_OPTIONS["allow_invalid_certificates"]
        else:
            _store.CLIENT_OPTIONS["allow_invalid_certificates"] = previous
        _store.reset()
        assert child.stdin is not None
        child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def _serve_tree(root: Path, latency: float) -> None:
    """The child side of :func:`serving_tree`: serve, then answer on stdin."""
    with serving() as server:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            wait = 0.0 if path.suffix == ".json" else latency
            route = Route(body=path.read_bytes(), latency=wait)
            server.routes["/" + path.relative_to(root).as_posix()] = route
        server.counted = lambda path: not path.endswith(DOCUMENT_SUFFIXES)
        print(json.dumps({"base": server.base, "ca_pem": server.ca_pem.decode()}), flush=True)
        for line in sys.stdin:
            command = line.strip()
            if command == "seen":
                seen = [[s.method, s.path, s.headers] for s in list(server.seen)]
                reply: dict[str, Any] = {"seen": seen}
            elif command == "peak":
                reply = {"peak": server.peak_in_flight}
            elif command.startswith("fail "):
                _, path, count, agent = command.split(" ", 3)
                failing = server.routes[path]
                failing.fail_first, failing.fail_agent = int(count), agent
                reply = {}
            else:
                server.seen.clear()
                server.peak_in_flight = 0
                reply = {}
            print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    _serve_tree(Path(sys.argv[1]), float(sys.argv[2]))
