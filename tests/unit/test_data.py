"""Unit tests for the data pass wiring — validator injection, no bytes, no deps.

These exercise :func:`rashid.data.validate_data` and its runner/CLI wiring with a
fake validator, exactly as ``test_schema.py`` does for the schema pass: the
byte-reading and geospatial stack are not touched here (that is
``test_data_checks``/``test_data_catalog``), so these run without the
geospatial stack (now a core dependency, still lazily imported).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import rashid.data as data_pass
from rashid.catalog import CatalogGraph, Node
from rashid.config import RulesConfig
from rashid.data import (
    DAT_CHECKSUM,
    DAT_COG,
    DAT_COG_STATS,
    DAT_CONSISTENCY,
    DAT_FORMAT,
    DAT_GEOPARQUET_VERSION,
    DAT_MIRROR,
    DAT_NATIVE_CRS,
    DAT_ORDERING,
    DAT_OVERVIEWS,
    DAT_PARTITION_SCHEMA,
    DAT_ROWGROUP_SIZE,
    DAT_ROWGROUP_STATS,
    DAT_SIZE,
    DAT_TABULAR,
    DAT_TILE_SIZE,
    DAT_UNAVAILABLE,
    DAT_VALID_PERCENT,
    DAT_VECTOR_COLUMNS,
    DataDefect,
    validate_data,
)
from rashid.data.reader import AssetReader, Locator
from rashid.model import Severity
from rashid.runner import validate
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit


def _graph(catalog: CatalogBuilder) -> CatalogGraph:
    catalog.collection("roads").item("seg1")
    return CatalogGraph.load(catalog.write())


def _defect_on_items(
    rule_id: str = DAT_CHECKSUM, field: str | None = "file:checksum"
) -> data_pass.Validator:
    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "item":
            return []
        return [DataDefect(rule_id, Severity.ERROR, "asset 'data' bytes diverge", "data", field)]

    return check


def test_defect_maps_to_finding(catalog: CatalogBuilder) -> None:
    graph = _graph(catalog)

    findings = validate_data(graph, _defect_on_items())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == DAT_CHECKSUM
    assert finding.severity is Severity.ERROR
    assert finding.object_id == "seg1"
    assert finding.json_pointer == "/assets/data/file:checksum"
    assert finding.path.endswith("seg1.json")


def test_defect_without_field_points_at_asset(catalog: CatalogBuilder) -> None:
    findings = validate_data(_graph(catalog), _defect_on_items(DAT_CONSISTENCY, field=None))

    assert findings[0].json_pointer == "/assets/data"


def test_no_defects_is_clean(catalog: CatalogBuilder) -> None:
    def clean(node: Node, reader: AssetReader) -> list[DataDefect]:
        return []

    assert validate_data(_graph(catalog), clean) == []


def test_systemic_failure_reported_once(catalog: CatalogBuilder) -> None:
    def boom(node: Node, reader: AssetReader) -> list[DataDefect]:
        raise RuntimeError("reader exploded")

    findings = validate_data(_graph(catalog), boom)

    assert len(findings) == 1
    assert findings[0].rule_id == DAT_UNAVAILABLE
    assert findings[0].severity is Severity.WARNING
    assert "reader exploded" in findings[0].message


def _items_graph(catalog: CatalogBuilder, count: int) -> CatalogGraph:
    collection = catalog.collection("roads")
    for i in range(count):
        collection.item(f"seg{i}")
    return CatalogGraph.load(catalog.write())


def _flag(node: Node) -> list[DataDefect]:
    return [DataDefect(DAT_CHECKSUM, Severity.ERROR, f"{node.id} diverges", "data")]


def test_findings_keep_catalog_order_when_objects_finish_out_of_order(
    catalog: CatalogBuilder,
) -> None:
    """The first object waits for the last one, so the pool must run them at once.

    A serial loop would block on the first object until the wait times out,
    which this test reports as a failure rather than a hang.
    """
    graph = _items_graph(catalog, 6)
    last_done = threading.Event()

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.id == "seg0" and not last_done.wait(timeout=10):
            raise AssertionError("seg5 never ran while seg0 waited")
        if node.id == "seg5":
            last_done.set()
        return _flag(node) if node.kind == "item" else []

    findings = validate_data(graph, check)

    assert [f.object_id for f in findings] == [f"seg{i}" for i in range(6)]


class _HrefReader:
    """Locates ``remote.tif`` as an https URL and ``local.tif`` as a file."""

    def locate(self, node: Node, href: str) -> Locator | None:
        if href == "remote.tif":
            return Locator(is_remote=True, source="https://example.org/remote.tif")
        if href == "local.tif":
            return Locator(is_remote=False, source="/nowhere/local.tif")
        return None

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        return None


def test_objects_with_a_remote_asset_run_on_the_remote_pool(catalog: CatalogBuilder) -> None:
    roads = catalog.collection("roads")
    roads.item("far", assets={"data": {"href": "remote.tif"}})
    roads.item("near", assets={"data": {"href": "local.tif"}})
    roads.item("both", assets={"a": {"href": "local.tif"}, "b": {"href": "remote.tif"}})
    roads.item("bare")
    graph = CatalogGraph.load(catalog.write())
    pools: dict[str | None, str] = {}

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        pools[node.id] = threading.current_thread().name.rsplit("_", 1)[0]
        return []

    validate_data(graph, check, reader_factory=lambda graph: _HrefReader())

    assert pools == {
        "roads": "data-local",
        "far": "data-remote",
        "near": "data-local",
        "both": "data-remote",
        "bare": "data-local",
    }


def test_a_reader_that_fails_to_locate_is_reported_once(catalog: CatalogBuilder) -> None:
    """Choosing a pool must not raise what the validator would report."""
    catalog.collection("roads").item("seg1", assets={"data": {"href": "x.tif"}})
    graph = CatalogGraph.load(catalog.write())

    class Broken(_HrefReader):
        def locate(self, node: Node, href: str) -> Locator | None:
            raise OSError("cannot resolve")

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        for asset in node.data.get("assets", {}).values():
            reader.locate(node, asset["href"])
        return []

    findings = validate_data(graph, check, reader_factory=lambda graph: Broken())

    assert [f.rule_id for f in findings] == [DAT_UNAVAILABLE]
    assert "cannot resolve" in findings[0].message


def test_systemic_failure_returns_without_waiting_on_running_objects(
    catalog: CatalogBuilder,
) -> None:
    """A failure is reported at once, not after every object in flight finishes."""
    graph = _items_graph(catalog, 2)
    release = threading.Event()

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.id == "seg0":
            raise RuntimeError("reader exploded")
        if node.id == "seg1":
            release.wait(timeout=30)
        return []

    started = time.monotonic()
    try:
        findings = validate_data(graph, check)
    finally:
        release.set()

    assert time.monotonic() - started < 10
    assert [f.rule_id for f in findings] == [DAT_UNAVAILABLE]


class _EndlessReader(_HrefReader):
    """Streams chunks forever, as a multi-gigabyte asset seems to a checksum."""

    def stream(self, node: Node, href: str) -> Iterator[bytes] | None:
        def chunks() -> Iterator[bytes]:
            while True:
                yield b"x" * 1024

        return chunks()


def test_systemic_failure_stops_a_running_stream(catalog: CatalogBuilder) -> None:
    """An object still streaming stops at its next chunk once the pass returns.

    The pool threads are not daemons, so the interpreter joins them at exit.
    A stream that ran on would hold the process open after the report.
    """
    graph = _items_graph(catalog, 2)
    streaming = threading.Event()
    stopped: list[BaseException] = []
    done = threading.Event()

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.id == "seg0":
            streaming.wait(timeout=10)
            raise RuntimeError("reader exploded")
        if node.id == "seg1":
            stream = reader.stream(node, "remote.tif")
            assert stream is not None
            try:
                for _chunk in stream:
                    streaming.set()
            except BaseException as exc:
                stopped.append(exc)
                raise
            finally:
                done.set()
        return []

    findings = validate_data(graph, check, reader_factory=lambda graph: _EndlessReader())

    assert [f.rule_id for f in findings] == [DAT_UNAVAILABLE]
    assert done.wait(timeout=10), "the stream ran on after the pass returned"
    assert "ended" in str(stopped[0])


def test_systemic_failure_keeps_earlier_findings_and_drops_later_ones(
    catalog: CatalogBuilder,
) -> None:
    graph = _items_graph(catalog, 3)

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.id == "seg1":
            raise RuntimeError("reader exploded")
        return _flag(node) if node.kind == "item" else []

    findings = validate_data(graph, check)

    assert [(f.rule_id, f.object_id) for f in findings] == [
        (DAT_CHECKSUM, "seg0"),
        (DAT_UNAVAILABLE, None),
    ]


def test_missing_geospatial_stack_downgrades_to_warning(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_import(graph: CatalogGraph | None = None) -> data_pass.Validator:
        raise ImportError("No module named 'pyarrow'")

    monkeypatch.setattr(data_pass, "default_validator", raise_import)

    findings = validate_data(_graph(catalog))

    assert len(findings) == 1
    assert findings[0].rule_id == DAT_UNAVAILABLE
    assert "geospatial stack" in findings[0].message
    assert "--no-data" in findings[0].message


def test_data_pass_surfaces_by_default(catalog: CatalogBuilder) -> None:
    """The data pass is on by default; an injected defect surfaces without data=True."""
    root = _graph(catalog).root_path
    report = validate(root, data_validator=_defect_on_items())
    assert any(f.rule_id == DAT_CHECKSUM for f in report.findings)


def test_disabling_one_rule_silences_only_it(catalog: CatalogBuilder) -> None:
    graph_root = _graph(catalog).root_path

    def two_defects(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "item":
            return []
        return [
            DataDefect(DAT_CHECKSUM, Severity.ERROR, "checksum", "data", "file:checksum"),
            DataDefect(DAT_SIZE, Severity.ERROR, "size", "data", "file:size"),
        ]

    report = validate(
        graph_root,
        config=RulesConfig(disabled=frozenset({DAT_SIZE})),
        data=True,
        data_validator=two_defects,
    )
    ids = {f.rule_id for f in report.findings}
    assert DAT_CHECKSUM in ids
    assert DAT_SIZE not in ids


def test_disabling_all_rules_skips_pass(catalog: CatalogBuilder) -> None:
    graph_root = _graph(catalog).root_path
    called = False

    def spy(node: Node, reader: AssetReader) -> list[DataDefect]:
        nonlocal called
        called = True
        return []

    all_ids = frozenset(
        {
            DAT_CHECKSUM,
            DAT_SIZE,
            DAT_FORMAT,
            DAT_COG,
            DAT_CONSISTENCY,
            DAT_ORDERING,
            DAT_ROWGROUP_STATS,
            DAT_ROWGROUP_SIZE,
            DAT_COG_STATS,
            DAT_VALID_PERCENT,
            DAT_OVERVIEWS,
            DAT_GEOPARQUET_VERSION,
            DAT_TILE_SIZE,
            DAT_PARTITION_SCHEMA,
            DAT_TABULAR,
            DAT_MIRROR,
            DAT_VECTOR_COLUMNS,
            DAT_NATIVE_CRS,
        }
    )
    validate(graph_root, config=RulesConfig(disabled=all_ids), data=True, data_validator=spy)

    assert called is False


def test_parse_error_nodes_are_skipped(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _graph(catalog).root_path
    # Corrupt the collection JSON so its node carries a parse_error.
    (root / "roads" / "collection.json").write_text("{ not json", encoding="utf-8")
    graph = CatalogGraph.load(root)

    seen: list[str] = []

    def record(node: Node, reader: AssetReader) -> list[DataDefect]:
        seen.append(node.kind)
        return []

    validate_data(graph, record)
    assert "unknown" not in seen  # the unparseable collection is not handed to the validator


def test_defect_with_explicit_pointer_keeps_it(catalog: CatalogBuilder) -> None:
    # Node-level defects (the partition-schema check) bind to no asset and
    # carry their own pointer instead of the /assets/<key> construction.
    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "collection":
            return []
        return [
            DataDefect(
                DAT_PARTITION_SCHEMA,
                Severity.ERROR,
                "partition schemas diverge",
                "",
                json_pointer="/partition:glob",
            )
        ]

    findings = validate_data(_graph(catalog), check)

    assert len(findings) == 1
    assert findings[0].rule_id == DAT_PARTITION_SCHEMA
    assert findings[0].json_pointer == "/partition:glob"


def test_data_pass_runs_by_default(catalog: CatalogBuilder) -> None:
    """Byte verification is the default; no data=True opt-in required."""
    catalog.collection("roads")
    root = catalog.write()
    calls: list[object] = []

    def spy(node: object, reader: object) -> list:
        calls.append(node)
        return []

    validate(root, structural=False, data_validator=spy)
    assert calls, "the data pass did not run under the default configuration"


def test_data_false_skips_the_pass(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    calls: list[object] = []

    def spy(node: object, reader: object) -> list:
        calls.append(node)
        return []

    validate(root, structural=False, data=False, data_validator=spy)
    assert calls == []
