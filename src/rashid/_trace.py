"""Counters, stage spans, and logging setup, from the standard library alone.

This module imports nothing from its own package. The package name and the
environment variable prefix come from ``__name__``, so the same file serves
any package that copies it (portolan-sdi/portolan-ops#131). For rashid the
variables are:

``RASHID_LOG``
    ``debug``, ``info``, ``warning``, or ``error``. Log records at that level
    and above go to stderr. stdout never receives a log record.
``RASHID_LOG_FILE``
    A path. Every record at DEBUG and above is appended to it as one JSON
    object per line, with the run id.
``RASHID_TIMINGS``
    ``1`` turns spans on. ``mem`` also starts :mod:`tracemalloc` and records
    its high-water mark at the end of each span. ``0``, ``false``, ``no``,
    and ``off`` leave spans off.
``RASHID_TRACE``
    A path. Spans are turned on, and a Chrome trace is written there at exit.
    https://ui.perfetto.dev opens it.

Counters are always on. :func:`count` takes one lock and one dict update, so
a call on every request or every parsed document costs well under a
microsecond. Tests read counters through :func:`counting` and assert exact
values, which turns a claim that a change does less work into pass or fail.

Spans are off by default. Then :func:`span` returns one shared
``nullcontext`` and costs one flag check. When on, each span records wall
time, CPU time, and the process RSS high-water mark at its end. A span on the
main thread counts the CPU of every thread, so a stage includes the work of
its pool. A span on another thread counts that thread's CPU only. A span nests under the span that is open in the same
:mod:`contextvars` context. A thread pool worker starts in an empty context,
so work goes through :func:`submit` or :func:`pool_map` to keep its parent.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, TypeVar

if TYPE_CHECKING:
    from concurrent.futures import Executor, Future

_PACKAGE = __name__.split(".")[0]
_PREFIX = _PACKAGE.upper()

ENV_LOG = f"{_PREFIX}_LOG"
ENV_LOG_FILE = f"{_PREFIX}_LOG_FILE"
ENV_TIMINGS = f"{_PREFIX}_TIMINGS"
ENV_TRACE = f"{_PREFIX}_TRACE"

#: One id per process, written into every JSON log line and the ``meta`` block.
RUN_ID = os.urandom(6).hex()

_START_NS = time.perf_counter_ns()
_START_WALL = time.time()

_T = TypeVar("_T")
_R = TypeVar("_R")

# One lock guards the counters, the span list, and the span ids. Pool workers
# write all three from many threads.
_lock = threading.Lock()


# --- counters ----------------------------------------------------------------

_counts: dict[str, int] = {}


def count(name: str, n: int = 1) -> None:
    """Add ``n`` to the counter ``name``."""
    with _lock:
        _counts[name] = _counts.get(name, 0) + n


def counters() -> dict[str, int]:
    """A snapshot of every counter, sorted by name."""
    with _lock:
        return dict(sorted(_counts.items()))


@contextlib.contextmanager
def counting() -> Iterator[dict[str, int]]:
    """Yield a dict that holds, on exit, each counter that changed inside the block."""
    before = counters()
    delta: dict[str, int] = {}
    try:
        yield delta
    finally:
        for name, value in counters().items():
            change = value - before.get(name, 0)
            if change:
                delta[name] = change


# --- spans -------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One finished span. Times are in nanoseconds; ``start_ns`` counts from import."""

    id: int
    parent: int | None
    name: str
    thread_id: int
    thread_name: str
    start_ns: int
    wall_ns: int
    cpu_ns: int
    peak_rss_bytes: int | None
    traced_peak_bytes: int | None
    attrs: dict[str, Any] = field(default_factory=dict)


_MODES = (None, "on", "mem")
_mode: str | None = None
_spans: list[Span] = []
_next_id = 0
_current: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    f"{_PACKAGE}_span", default=None
)
_OFF: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
_started_tracemalloc = False


def enable(mode: str | None = "on") -> None:
    """Turn spans on (``"on"``), on with tracemalloc (``"mem"``), or off (None)."""
    global _mode, _started_tracemalloc
    if mode not in _MODES:
        raise ValueError(f"timings mode must be one of {_MODES}, got {mode!r}")
    # tracemalloc imports pickle; load it only when memory is measured.
    if mode == "mem":
        import tracemalloc

        if not tracemalloc.is_tracing():
            tracemalloc.start()
            _started_tracemalloc = True
    elif _started_tracemalloc:
        import tracemalloc

        tracemalloc.stop()
        _started_tracemalloc = False
    _mode = mode


def enabled() -> bool:
    """Whether spans are being recorded."""
    return _mode is not None


def reset() -> None:
    """Clear the counters and the recorded spans. The mode stays as it is."""
    with _lock:
        _counts.clear()
        _spans.clear()


def span(name: str, **attrs: Any) -> contextlib.AbstractContextManager[None]:
    """Time the block as one span called ``name``, with ``attrs`` attached."""
    if _mode is None:
        return _OFF
    return _recording(name, attrs)


@contextlib.contextmanager
def _recording(name: str, attrs: dict[str, Any]) -> Iterator[None]:
    global _next_id
    with _lock:
        _next_id += 1
        span_id = _next_id
    parent = _current.get()
    token = _current.set(span_id)
    start = time.perf_counter_ns()
    # On the main thread a stage's CPU includes its pool's work. On a worker
    # the process clock would count every other worker too.
    clock = (
        time.process_time_ns
        if threading.current_thread() is threading.main_thread()
        else time.thread_time_ns
    )
    cpu = clock()
    try:
        yield
    finally:
        wall = time.perf_counter_ns() - start
        cpu = clock() - cpu
        _current.reset(token)
        thread = threading.current_thread()
        record = Span(
            id=span_id,
            parent=parent,
            name=name,
            thread_id=thread.ident or 0,
            thread_name=thread.name,
            start_ns=start - _START_NS,
            wall_ns=wall,
            cpu_ns=cpu,
            peak_rss_bytes=peak_rss_bytes(),
            traced_peak_bytes=_traced_peak(),
            attrs=attrs,
        )
        with _lock:
            _spans.append(record)


def _traced_peak() -> int | None:
    """The tracemalloc high-water mark, or None when nothing traces (no import needed)."""
    module = sys.modules.get("tracemalloc")
    if module is None or not module.is_tracing():
        return None
    peak: int = module.get_traced_memory()[1]
    return peak


def spans() -> list[Span]:
    """Every finished span, in the order each finished."""
    with _lock:
        return list(_spans)


def submit(pool: Executor, fn: Callable[..., _R], /, *args: Any) -> Future[_R]:
    """``pool.submit(fn, *args)``, run in a copy of the caller's context.

    A span opened inside ``fn`` then nests under the span open at the call.
    Each call copies the context once, because one context cannot be entered
    by two threads at the same time.
    """
    return pool.submit(contextvars.copy_context().run, fn, *args)


def pool_map(pool: Executor, fn: Callable[[_T], _R], items: Iterable[_T]) -> list[_R]:
    """``list(pool.map(fn, items))``, with each call in a copy of the caller's context."""
    futures = [submit(pool, fn, item) for item in items]
    return [future.result() for future in futures]


def _rss_bytes(raw: int, platform: str) -> int:
    """``ru_maxrss`` in bytes. macOS reports bytes; Linux and the BSDs report KiB."""
    return raw if platform == "darwin" else raw * 1024


def peak_rss_bytes() -> int | None:
    """The process RSS high-water mark, or None where :mod:`resource` is absent (Windows)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows only
        return None
    return _rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, sys.platform)


def _mb(value: int | None) -> float | None:
    return None if value is None else round(value / 1_048_576, 1)


def _path(record: Span, by_id: Mapping[int, Span]) -> tuple[str, ...]:
    names = [record.name]
    parent = record.parent
    while parent is not None and parent in by_id:
        names.append(by_id[parent].name)
        parent = by_id[parent].parent
    return tuple(reversed(names))


def timings() -> list[dict[str, Any]]:
    """The span tree, with sibling spans of one name merged into one node.

    A node holds the number of ``calls``, the summed ``wall_ms`` and ``cpu_ms``,
    and the largest ``peak_rss_mb`` (and ``traced_peak_mb`` under ``mem``).
    Ten thousand ``http.get`` spans under ``crawl`` become one node. A node
    with one call keeps that call's ``attrs``.
    """
    records = sorted(spans(), key=lambda s: (s.start_ns, s.id))
    by_id = {record.id: record for record in records}
    roots: list[dict[str, Any]] = []
    index: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        path = _path(record, by_id)
        node = index.get(path)
        if node is None:
            node = {"name": record.name, "calls": 0, "wall_ms": 0.0, "cpu_ms": 0.0}
            node["peak_rss_mb"] = None
            node["children"] = []
            index[path] = node
            parent = index.get(path[:-1])
            (roots if parent is None else parent["children"]).append(node)
        node["calls"] += 1
        node["wall_ms"] += record.wall_ns / 1e6
        node["cpu_ms"] += record.cpu_ns / 1e6
        rss = _mb(record.peak_rss_bytes)
        if rss is not None and (node["peak_rss_mb"] is None or rss > node["peak_rss_mb"]):
            node["peak_rss_mb"] = rss
        traced = _mb(record.traced_peak_bytes)
        if traced is not None and traced > node.get("traced_peak_mb", -1.0):
            node["traced_peak_mb"] = traced
        node["attrs"] = dict(record.attrs) if node["calls"] == 1 else None
    for node in index.values():
        node["wall_ms"] = round(node["wall_ms"], 3)
        node["cpu_ms"] = round(node["cpu_ms"], 3)
        if not node["attrs"]:
            del node["attrs"]
    return roots


def format_timings(tree: list[dict[str, Any]]) -> str:
    """The span tree as a fixed-width table, children indented under parents."""
    rows: list[tuple[str, str, str, str, str]] = []

    def walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for node in nodes:
            rss = node["peak_rss_mb"]
            rows.append(
                (
                    "  " * depth + node["name"],
                    str(node["calls"]),
                    f"{node['wall_ms']:.1f}",
                    f"{node['cpu_ms']:.1f}",
                    "-" if rss is None else f"{rss:.1f}",
                )
            )
            walk(node["children"], depth + 1)

    walk(tree, 0)
    header = ("span", "calls", "wall ms", "cpu ms", "peak rss MB")
    width = max([len(header[0])] + [len(row[0]) for row in rows])
    lines = [f"{header[0]:<{width}}  " + "  ".join(f"{h:>11}" for h in header[1:])]
    lines += [f"{row[0]:<{width}}  " + "  ".join(f"{c:>11}" for c in row[1:]) for row in rows]
    return "\n".join(lines)


def meta(version: str) -> dict[str, Any]:
    """The ``meta`` block a CLI adds to its JSON output when timings are on."""
    return {
        "version": version,
        "run_id": RUN_ID,
        "timings": timings(),
        "counters": counters(),
        "peak_rss_mb": _mb(peak_rss_bytes()),
    }


def chrome_trace() -> dict[str, Any]:
    """Every span as a Chrome trace event, plus one name event per thread."""
    pid = os.getpid()
    events: list[dict[str, Any]] = []
    threads: dict[int, str] = {}
    for record in spans():
        threads[record.thread_id] = record.thread_name
        events.append(
            {
                "name": record.name,
                "cat": _PACKAGE,
                "ph": "X",
                "ts": record.start_ns / 1000,
                "dur": record.wall_ns / 1000,
                "pid": pid,
                "tid": record.thread_id,
                "args": record.attrs,
            }
        )
    for tid, name in sorted(threads.items()):
        events.append(
            {"name": "thread_name", "ph": "M", "pid": pid, "tid": tid, "args": {"name": name}}
        )
    return {"traceEvents": events, "displayTimeUnit": "ms"}


def write_chrome_trace(path: str | os.PathLike[str]) -> None:
    """Write :func:`chrome_trace` to ``path``. Values JSON cannot hold are written as strings."""
    Path(path).write_text(json.dumps(chrome_trace(), default=str), encoding="utf-8")


_trace_paths: list[str] = []


def trace_to(path: str) -> None:
    """Turn spans on, and write the Chrome trace to ``path`` when the process exits.

    Raises :class:`FileNotFoundError` when the directory of ``path`` does not
    exist, so a typo fails before the run rather than after it.
    """
    parent = Path(path).parent
    if not parent.is_dir():
        raise FileNotFoundError(f"trace directory {str(parent)!r} does not exist")
    if _mode is None:
        enable("on")
    if not _trace_paths:
        atexit.register(_write_traces)
    if path not in _trace_paths:
        _trace_paths.append(path)


def _write_traces() -> None:
    for path in _trace_paths:
        try:
            write_chrome_trace(path)
        except OSError as exc:
            print(f"{_PACKAGE}: cannot write trace {path}: {exc}", file=sys.stderr)


# --- logging -----------------------------------------------------------------

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class _Elapsed(logging.Formatter):
    """``+3.214s DEBUG rashid._store: message``. The time counts from import."""

    def format(self, record: logging.LogRecord) -> str:
        line = (
            f"+{record.created - _START_WALL:.3f}s {record.levelname} "
            f"{record.name}: {record.getMessage()}"
        )
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class _JsonLines(logging.Formatter):
    """One JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": record.created,
            "elapsed_s": round(record.created - _START_WALL, 6),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "thread": record.threadName,
            "run_id": RUN_ID,
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _Stderr(logging.StreamHandler):  # type: ignore[type-arg]
    """A stream handler that looks up ``sys.stderr`` at each write.

    A test runner, or Click's ``CliRunner``, swaps ``sys.stderr`` after the
    handler exists. A handler bound at creation would write to the old stream.
    """

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass


_installed: list[logging.Handler] = []
# Whether configure_logging turned on logging.captureWarnings, and so must turn it off.
_captured_warnings = False

# Values of ``*_TIMINGS`` that leave spans off.
_FALSE = ("", "0", "false", "no", "off")

# Messages warn_once already printed.
_warned: set[str] = set()


def warn_once(message: str) -> None:
    """Print ``message`` to stderr, prefixed with the package name, once per process.

    The import and a CLI can both find the same bad variable. The user then
    reads one line, not two.
    """
    if message not in _warned:
        _warned.add(message)
        print(f"{_PACKAGE}: {message}", file=sys.stderr)


def parse_level(name: str) -> int:
    """The :mod:`logging` level for ``debug``, ``info``, ``warning``, or ``error``."""
    try:
        return _LEVELS[name.strip().lower()]
    except KeyError:
        raise ValueError(f"log level must be one of {', '.join(_LEVELS)}, got {name!r}") from None


def configure_logging(level: str | None, log_file: str | None = None) -> None:
    """Send the package's log records to stderr at ``level``, and all of them to ``log_file``.

    Calling it again replaces the handlers the last call installed. With both
    arguments None it removes them, and the package logger falls back to its
    ``NullHandler``. stderr gets WARNING and above when only ``log_file`` is set.

    At DEBUG, or with ``log_file``, Python warnings are logged too. They go
    through the ``py.warnings`` logger, which gets the same handlers.
    """
    global _captured_warnings
    logger = logging.getLogger(_PACKAGE)
    warnings_logger = logging.getLogger("py.warnings")
    for handler in _installed:
        logger.removeHandler(handler)
        warnings_logger.removeHandler(handler)
        handler.close()
    _installed.clear()
    if _captured_warnings:
        logging.captureWarnings(False)
        _captured_warnings = False
    if level is None and log_file is None:
        logger.setLevel(logging.NOTSET)
        return
    threshold = parse_level(level or "warning")
    stderr = _Stderr()
    stderr.setLevel(threshold)
    stderr.setFormatter(_Elapsed())
    _installed.append(stderr)
    if log_file is not None:
        jsonl = logging.FileHandler(log_file, encoding="utf-8")
        jsonl.setLevel(logging.DEBUG)
        jsonl.setFormatter(_JsonLines())
        _installed.append(jsonl)
    for handler in _installed:
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if log_file is not None else threshold)
    if threshold <= logging.DEBUG or log_file is not None:
        logging.captureWarnings(True)
        _captured_warnings = True
        for handler in _installed:
            warnings_logger.addHandler(handler)


def env_timings(environ: Mapping[str, str] = os.environ) -> str | None:
    """The spans mode that ``*_TIMINGS`` asks for: ``"on"``, ``"mem"``, or None."""
    value = (environ.get(ENV_TIMINGS) or "").strip().lower()
    if value in _FALSE:
        return None
    return "mem" if value == "mem" else "on"


def configure_from_env(environ: Mapping[str, str] = os.environ) -> None:
    """Apply the ``*_LOG``, ``*_LOG_FILE``, ``*_TIMINGS``, and ``*_TRACE`` variables.

    An unset or empty variable changes nothing. An invalid value prints one
    line to stderr and is ignored, so a typo never stops an import.
    """
    level = environ.get(ENV_LOG) or None
    log_file = environ.get(ENV_LOG_FILE) or None
    if level is not None or log_file is not None:
        try:
            configure_logging(level, log_file)
        except (ValueError, OSError) as exc:
            warn_once(f"ignoring {ENV_LOG}/{ENV_LOG_FILE}: {exc}")
    timings_mode = env_timings(environ)
    if timings_mode is not None:
        enable(timings_mode)
    trace_path = environ.get(ENV_TRACE) or None
    if trace_path is not None:
        try:
            trace_to(trace_path)
        except OSError as exc:
            warn_once(f"ignoring {ENV_TRACE}: {exc}")
