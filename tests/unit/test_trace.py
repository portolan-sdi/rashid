"""Unit tests for rashid._trace: counters, spans, the Chrome trace, and logging setup.

The module is stdlib-only and names no rashid module, so the same file can
serve another package. Each test here checks one behavior a caller relies on:
exact counts from many threads, a free span when timings are off, parent
spans that survive a thread pool, and log records that reach stderr only.
"""

from __future__ import annotations

import ast
import contextlib
import io
import itertools
import json
import logging
import sys
import threading
import time
import tracemalloc
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rashid import _trace


def test_count_adds_and_defaults_to_one() -> None:
    _trace.count("a")
    _trace.count("a")
    _trace.count("b", 5)
    assert _trace.counters() == {"a": 2, "b": 5}


def test_counters_are_sorted_and_a_snapshot() -> None:
    _trace.count("z")
    _trace.count("a")
    snapshot = _trace.counters()
    assert list(snapshot) == ["a", "z"]
    _trace.count("a")
    assert snapshot["a"] == 1


def test_count_is_exact_under_many_threads() -> None:
    def work() -> None:
        for _ in range(1000):
            _trace.count("hits")

    threads = [threading.Thread(target=work) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert _trace.counters()["hits"] == 32_000


def test_counting_yields_only_what_changed_inside() -> None:
    _trace.count("before", 3)
    with _trace.counting() as delta:
        _trace.count("before", 2)
        _trace.count("inside")
        assert delta == {}
    assert delta == {"before": 2, "inside": 1}


def test_counting_records_the_delta_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), _trace.counting() as delta:
        _trace.count("x")
        raise RuntimeError
    assert delta == {"x": 1}


def test_reset_clears_counters_and_spans_but_keeps_the_mode() -> None:
    _trace.enable("on")
    _trace.count("x")
    with _trace.span("s"):
        pass
    _trace.reset()
    assert _trace.counters() == {}
    assert _trace.spans() == []
    assert _trace.enabled()


def test_span_is_one_shared_nullcontext_when_off() -> None:
    assert not _trace.enabled()
    first = _trace.span("a", n=1)
    assert first is _trace.span("b")
    assert isinstance(first, contextlib.nullcontext)
    with first:
        pass
    assert _trace.spans() == []


def test_span_records_times_thread_and_attrs() -> None:
    _trace.enable("on")
    with _trace.span("stage", files=3):
        sum(range(10_000))
    (record,) = _trace.spans()
    assert record.name == "stage"
    assert record.parent is None
    assert record.attrs == {"files": 3}
    assert record.wall_ns > 0
    assert record.cpu_ns >= 0
    assert record.start_ns >= 0
    assert record.thread_id == threading.get_ident()
    assert record.thread_name == threading.current_thread().name
    assert record.traced_peak_bytes is None


def test_span_is_recorded_when_the_block_raises() -> None:
    _trace.enable("on")
    with pytest.raises(ValueError), _trace.span("boom"):
        raise ValueError
    assert [s.name for s in _trace.spans()] == ["boom"]


def test_spans_nest_and_ids_are_unique() -> None:
    _trace.enable("on")
    with _trace.span("outer"):
        with _trace.span("inner"):
            pass
        with _trace.span("inner"):
            pass
    inner_a, inner_b, outer = _trace.spans()
    assert outer.parent is None
    assert inner_a.parent == outer.id
    assert inner_b.parent == outer.id
    assert len({inner_a.id, inner_b.id, outer.id}) == 3


def test_a_plain_pool_loses_the_parent_and_pool_map_keeps_it() -> None:
    _trace.enable("on")

    def leaf(n: int) -> int:
        with _trace.span("leaf"):
            return n * 2

    with _trace.span("root"), ThreadPoolExecutor(max_workers=4) as pool:
        plain = list(pool.map(leaf, range(4)))
        kept = _trace.pool_map(pool, leaf, range(4))
    assert plain == kept == [0, 2, 4, 6]
    root = next(s for s in _trace.spans() if s.name == "root")
    parents = [s.parent for s in _trace.spans() if s.name == "leaf"]
    assert parents.count(None) == 4
    assert parents.count(root.id) == 4


def test_a_worker_span_counts_its_own_thread_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main thread reads the process clock; a worker reads its thread clock."""
    process, thread_clock = itertools.count(0, 1000), itertools.count(0, 7)
    monkeypatch.setattr(_trace.time, "process_time_ns", lambda: next(process))
    monkeypatch.setattr(_trace.time, "thread_time_ns", lambda: next(thread_clock))
    _trace.enable("on")
    with _trace.span("main"):
        pass

    def work() -> None:
        with _trace.span("worker"):
            pass

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    clocks = {s.name: s.cpu_ns for s in _trace.spans()}
    assert clocks == {"main": 1000, "worker": 7}


def test_worker_cpu_excludes_other_threads() -> None:
    _trace.enable("on")
    stop = threading.Event()

    def burn() -> None:
        while not stop.is_set():
            sum(range(1000))

    def idle() -> None:
        with _trace.span("idle"):
            time.sleep(0.2)

    burner = threading.Thread(target=burn)
    burner.start()
    try:
        worker = threading.Thread(target=idle)
        worker.start()
        worker.join()
    finally:
        stop.set()
        burner.join()
    (record,) = _trace.spans()
    assert record.wall_ns >= 150_000_000
    assert record.cpu_ns < record.wall_ns / 4


def test_submit_passes_arguments_and_returns_a_future() -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert _trace.submit(pool, pow, 2, 10).result() == 1024


def test_mem_mode_starts_tracemalloc_and_off_stops_it() -> None:
    assert not tracemalloc.is_tracing()
    _trace.enable("mem")
    try:
        assert tracemalloc.is_tracing()
        with _trace.span("alloc"):
            data = [bytes(1000) for _ in range(100)]
        assert data
        (record,) = _trace.spans()
        assert record.traced_peak_bytes is not None
        assert record.traced_peak_bytes >= 100_000
    finally:
        _trace.enable(None)
    assert not tracemalloc.is_tracing()


def test_mem_mode_leaves_a_caller_started_tracemalloc_running() -> None:
    tracemalloc.start()
    try:
        _trace.enable("mem")
        _trace.enable(None)
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_enable_refuses_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="timings mode"):
        _trace.enable("loud")


@pytest.mark.parametrize(
    ("raw", "platform", "expected"),
    [(2048, "linux", 2048 * 1024), (2048, "darwin", 2048), (7, "freebsd", 7 * 1024)],
)
def test_rss_is_normalised_to_bytes(raw: int, platform: str, expected: int) -> None:
    assert _trace._rss_bytes(raw, platform) == expected


def test_peak_rss_is_a_plausible_byte_count() -> None:
    peak = _trace.peak_rss_bytes()
    if peak is None:  # pragma: no cover - Windows
        pytest.skip("no resource module on this platform")
    assert 1_000_000 < peak < 1_000_000_000_000


def _span(
    span_id: int, name: str, parent: int | None, start: int, wall: int, **attrs: object
) -> _trace.Span:
    return _trace.Span(
        id=span_id,
        parent=parent,
        name=name,
        thread_id=1,
        thread_name="MainThread",
        start_ns=start,
        wall_ns=wall,
        cpu_ns=wall // 2,
        peak_rss_bytes=span_id * 1_048_576,
        traced_peak_bytes=None,
        attrs=dict(attrs),
    )


def test_timings_merge_siblings_of_one_name(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        _span(2, "http.get", 1, 10, 1_000_000),
        _span(3, "http.get", 1, 20, 3_000_000),
        _span(4, "parse", None, 50, 500_000, files=2),
        _span(1, "crawl", None, 0, 10_000_000),
    ]
    monkeypatch.setattr(_trace, "_spans", records)
    assert _trace.timings() == [
        {
            "name": "crawl",
            "calls": 1,
            "wall_ms": 10.0,
            "cpu_ms": 5.0,
            "peak_rss_mb": 1.0,
            "children": [
                {
                    "name": "http.get",
                    "calls": 2,
                    "wall_ms": 4.0,
                    "cpu_ms": 2.0,
                    "peak_rss_mb": 3.0,
                    "children": [],
                }
            ],
        },
        {
            "name": "parse",
            "calls": 1,
            "wall_ms": 0.5,
            "cpu_ms": 0.25,
            "peak_rss_mb": 4.0,
            "children": [],
            "attrs": {"files": 2},
        },
    ]


def test_timings_report_the_traced_peak_under_mem(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _span(1, "load", None, 0, 1_000)
    traced = _trace.Span(**{**record.__dict__, "traced_peak_bytes": 3 * 1_048_576})
    monkeypatch.setattr(_trace, "_spans", [traced])
    assert _trace.timings()[0]["traced_peak_mb"] == 3.0


def test_timings_treat_a_span_whose_parent_is_missing_as_a_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_trace, "_spans", [_span(2, "orphan", 99, 0, 1_000)])
    assert [node["name"] for node in _trace.timings()] == ["orphan"]


def test_format_timings_indents_children() -> None:
    _trace.enable("on")
    with _trace.span("check"), _trace.span("rules"):
        pass
    table = _trace.format_timings(_trace.timings()).splitlines()
    assert table[0].split() == ["span", "calls", "wall", "ms", "cpu", "ms", "peak", "rss", "MB"]
    assert table[1].startswith("check ")
    assert table[2].startswith("  rules ")
    assert table[2].split()[1] == "1"


def test_format_timings_shows_a_dash_without_rss() -> None:
    node = {"name": "x", "calls": 1, "wall_ms": 1.0, "cpu_ms": 1.0, "peak_rss_mb": None}
    table = _trace.format_timings([{**node, "children": []}])
    assert table.splitlines()[1].split()[-1] == "-"


def test_meta_holds_version_run_id_timings_counters_and_rss() -> None:
    _trace.enable("on")
    _trace.count("json.parses", 4)
    with _trace.span("check"):
        pass
    meta = _trace.meta("9.9.9")
    assert meta["version"] == "9.9.9"
    assert meta["run_id"] == _trace.RUN_ID
    assert len(meta["run_id"]) == 12
    assert meta["counters"] == {"json.parses": 4}
    assert [node["name"] for node in meta["timings"]] == ["check"]
    assert meta["peak_rss_mb"] is None or meta["peak_rss_mb"] > 0
    json.dumps(meta)


def test_chrome_trace_has_complete_events_and_thread_names(tmp_path: Path) -> None:
    _trace.enable("on")
    with _trace.span("outer", path=Path("a/b")):

        def work() -> None:
            with _trace.span("worker"):
                pass

        thread = threading.Thread(target=work, name="pool-1")
        thread.start()
        thread.join()
    out = tmp_path / "trace.json"
    _trace.write_chrome_trace(out)
    trace = json.loads(out.read_text(encoding="utf-8"))
    assert trace["displayTimeUnit"] == "ms"
    complete = [e for e in trace["traceEvents"] if e["ph"] == "X"]
    named = {e["args"]["name"] for e in trace["traceEvents"] if e["ph"] == "M"}
    assert {e["name"] for e in complete} == {"outer", "worker"}
    assert named == {"pool-1", threading.current_thread().name}
    outer = next(e for e in complete if e["name"] == "outer")
    assert outer["cat"] == "rashid"
    assert outer["args"] == {"path": "a/b"}
    assert outer["dur"] > 0
    assert outer["ts"] >= 0


def test_trace_to_enables_spans_and_writes_each_path_once_at_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[object] = []
    monkeypatch.setattr(_trace, "_trace_paths", [])
    monkeypatch.setattr(_trace.atexit, "register", registered.append)
    out = str(tmp_path / "t.json")
    _trace.trace_to(out)
    _trace.trace_to(out)
    assert _trace.enabled()
    assert _trace._trace_paths == [out]
    assert registered == [_trace._write_traces]
    with _trace.span("x"):
        pass
    _trace._write_traces()
    assert json.loads(Path(out).read_text(encoding="utf-8"))["traceEvents"]


def test_trace_to_keeps_mem_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_trace, "_trace_paths", [])
    monkeypatch.setattr(_trace.atexit, "register", lambda fn: None)
    _trace.enable("mem")
    try:
        _trace.trace_to(str(tmp_path / "t.json"))
        assert _trace._mode == "mem"
    finally:
        _trace.enable(None)


@pytest.mark.parametrize(
    ("name", "level"),
    [
        ("debug", logging.DEBUG),
        ("INFO", logging.INFO),
        (" warning ", logging.WARNING),
        ("error", logging.ERROR),
    ],
)
def test_parse_level(name: str, level: int) -> None:
    assert _trace.parse_level(name) == level


def test_parse_level_refuses_an_unknown_name() -> None:
    with pytest.raises(ValueError, match="debug, info, warning, error"):
        _trace.parse_level("verbose")


def test_debug_records_reach_stderr_and_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _trace.configure_logging("debug")
    logging.getLogger("rashid.example").debug("hello %s", "there")
    captured = capsys.readouterr()
    assert captured.out == ""
    (line,) = captured.err.splitlines()
    assert line.startswith("+")
    assert line.split(" ", 1)[1] == "DEBUG rashid.example: hello there"
    assert line.split(" ", 1)[0].endswith("s")


def test_the_level_filters_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    _trace.configure_logging("warning")
    logger = logging.getLogger("rashid.example")
    logger.info("quiet")
    logger.warning("loud")
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().err.splitlines()] == [
        "WARNING rashid.example: loud"
    ]


def test_an_exception_is_formatted_under_the_line(capsys: pytest.CaptureFixture[str]) -> None:
    _trace.configure_logging("error")
    try:
        raise KeyError("k")
    except KeyError:
        logging.getLogger("rashid.example").exception("failed")
    err = capsys.readouterr().err
    assert "ERROR rashid.example: failed\nTraceback" in err
    assert "KeyError: 'k'" in err


def test_log_file_gets_every_record_as_json_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "run.jsonl"
    _trace.configure_logging(None, str(log_file))
    logger = logging.getLogger("rashid.example")
    logger.debug("detail %d", 1)
    logger.warning("visible")
    try:
        raise ValueError("bad")
    except ValueError:
        logger.exception("with traceback")
    _trace.configure_logging(None)
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert [e["message"] for e in entries] == ["detail 1", "visible", "with traceback"]
    assert [e["level"] for e in entries] == ["DEBUG", "WARNING", "ERROR"]
    assert {e["run_id"] for e in entries} == {_trace.RUN_ID}
    assert {e["logger"] for e in entries} == {"rashid.example"}
    assert all(e["elapsed_s"] >= 0 and e["time"] > 0 and e["thread"] for e in entries)
    assert "exception" not in entries[0]
    assert "ValueError: bad" in entries[2]["exception"]
    stderr = capsys.readouterr().err.splitlines()
    assert [line.split(" ", 1)[1] for line in stderr[:2]] == [
        "WARNING rashid.example: visible",
        "ERROR rashid.example: with traceback",
    ]


def test_configure_again_replaces_handlers_and_none_removes_them() -> None:
    logger = logging.getLogger("rashid")
    before = list(logger.handlers)
    _trace.configure_logging("debug")
    _trace.configure_logging("info")
    added = [h for h in logger.handlers if h not in before]
    assert len(added) == 1
    assert added[0].level == logging.INFO
    assert logger.level == logging.INFO
    _trace.configure_logging(None)
    assert logger.handlers == before
    assert logger.level == logging.NOTSET


def test_debug_captures_warnings_and_teardown_releases_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(_trace.logging, "captureWarnings", calls.append)
    monkeypatch.setattr(_trace, "_captured_warnings", False)
    _trace.configure_logging("info")
    assert calls == []
    _trace.configure_logging("debug")
    assert calls == [True]
    _trace.configure_logging(None)
    assert calls == [True, False]
    assert logging.getLogger("py.warnings").handlers == []


def test_a_captured_warning_reaches_stderr_and_the_log_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "run.jsonl"
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        _trace.configure_logging("debug", str(log_file))
        warnings.warn("old API", UserWarning, stacklevel=1)
        assert warnings.showwarning is logging._showwarning  # type: ignore[attr-defined]
        _trace.configure_logging(None)
        # Checked inside the block: on exit catch_warnings restores showwarning itself.
        assert warnings.showwarning is not logging._showwarning  # type: ignore[attr-defined]
    assert "WARNING py.warnings:" in capsys.readouterr().err
    (entry,) = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert entry["logger"] == "py.warnings"
    assert "UserWarning: old API" in entry["message"]


def test_stderr_handler_follows_a_swapped_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    _trace.configure_logging("info")
    swapped = io.StringIO()
    monkeypatch.setattr(sys, "stderr", swapped)
    logging.getLogger("rashid.example").info("late")
    assert swapped.getvalue().endswith("INFO rashid.example: late\n")


def test_stderr_handler_ignores_an_assigned_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _trace._Stderr()
    handler.setStream(io.StringIO())
    assert handler.stream is sys.stderr


def test_env_unset_changes_nothing() -> None:
    logger = logging.getLogger("rashid")
    before = list(logger.handlers)
    _trace.configure_from_env({})
    assert logger.handlers == before
    assert not _trace.enabled()


@pytest.mark.parametrize("value", ["", "0", "false", "No", "off"])
def test_env_false_timings_leave_spans_off(value: str) -> None:
    _trace.configure_from_env({"RASHID_TIMINGS": value})
    assert not _trace.enabled()


@pytest.mark.parametrize(("value", "mode"), [("1", "on"), ("yes", "on"), (" MEM ", "mem")])
def test_env_timings_turn_spans_on(value: str, mode: str) -> None:
    try:
        _trace.configure_from_env({"RASHID_TIMINGS": value})
        assert _trace._mode == mode
    finally:
        _trace.enable(None)


def test_env_log_and_log_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = tmp_path / "x.jsonl"
    _trace.configure_from_env({"RASHID_LOG": "info", "RASHID_LOG_FILE": str(log_file)})
    logger = logging.getLogger("rashid.example")
    logger.debug("file only")
    logger.info("both")
    _trace.configure_logging(None)
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == 2
    assert [line.split(" ", 1)[1] for line in capsys.readouterr().err.splitlines()] == [
        "INFO rashid.example: both"
    ]


def test_env_invalid_level_prints_one_line_and_continues(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _trace.configure_from_env({"RASHID_LOG": "chatty", "RASHID_TIMINGS": "1"})
    err = capsys.readouterr().err
    assert err.startswith("rashid: ignoring RASHID_LOG/RASHID_LOG_FILE: log level must be")
    assert err.count("\n") == 1
    assert _trace.enabled()


def test_env_trace_registers_the_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_trace, "_trace_paths", [])
    monkeypatch.setattr(_trace.atexit, "register", lambda fn: None)
    out = str(tmp_path / "t.json")
    _trace.configure_from_env({"RASHID_TRACE": out})
    assert _trace._trace_paths == [out]
    assert _trace.enabled()


def test_warn_once_prints_a_message_once(capsys: pytest.CaptureFixture[str]) -> None:
    _trace.warn_once("ignoring X: bad")
    _trace.warn_once("ignoring X: bad")
    _trace.warn_once("ignoring Y: bad")
    assert capsys.readouterr().err == "rashid: ignoring X: bad\nrashid: ignoring Y: bad\n"


def test_env_trace_into_a_missing_directory_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_trace, "_trace_paths", [])
    monkeypatch.setattr(_trace.atexit, "register", lambda fn: None)
    _trace.configure_from_env({"RASHID_TRACE": str(tmp_path / "missing" / "t.json")})
    err = capsys.readouterr().err
    assert err.startswith("rashid: ignoring RASHID_TRACE: trace directory")
    assert err.count("\n") == 1
    assert _trace._trace_paths == []


def test_a_failed_trace_write_at_exit_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.setattr(_trace, "_trace_paths", [str(gone / "t.json")])
    gone.rmdir()
    _trace._write_traces()
    assert capsys.readouterr().err.startswith("rashid: cannot write trace ")


@pytest.mark.parametrize(
    ("value", "mode"), [(None, None), ("off", None), ("1", "on"), ("Mem", "mem")]
)
def test_env_timings(value: str | None, mode: str | None) -> None:
    environ = {} if value is None else {"RASHID_TIMINGS": value}
    assert _trace.env_timings(environ) == mode


def test_env_names_derive_from_the_package() -> None:
    assert (_trace.ENV_LOG, _trace.ENV_LOG_FILE, _trace.ENV_TIMINGS, _trace.ENV_TRACE) == (
        "RASHID_LOG",
        "RASHID_LOG_FILE",
        "RASHID_TIMINGS",
        "RASHID_TRACE",
    )


def test_the_package_logger_has_a_null_handler() -> None:
    assert any(isinstance(h, logging.NullHandler) for h in logging.getLogger("rashid").handlers)


def test_the_module_imports_only_the_standard_library() -> None:
    """The file can be copied into another package unchanged (portolan-ops#131)."""
    tree = ast.parse(Path(_trace.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    # A mutmut sweep runs this test against its mutated copy of the file,
    # which imports mutmut's trampoline. The real file never imports it.
    assert imported - {"__future__", "mutmut"} <= sys.stdlib_module_names
