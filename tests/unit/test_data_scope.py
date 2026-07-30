"""Unit tests for data scope — which assets the data pass is allowed to read.

A metadata-only mirror declares terabytes of assets on someone else's host and
a handful of its own; ``--data-scope local`` checks the ones it owns and reads
nothing else (#86). These tests use a fake validator that streams every asset
and records what came back, so the scope wiring is covered without the
geospatial stack, exactly as ``test_data.py`` does. ``urlopen`` is replaced
throughout: a stub server for the default scope, a sentinel that fails the test
for the local one, which is how "zero remote bytes" is asserted rather than
assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import rashid.cli as cli_mod
import rashid.data.reader as reader_mod
from rashid.catalog import CatalogGraph, Node
from rashid.cli import main
from rashid.data import DataDefect, Validator, validate_data
from rashid.data.reader import AssetReader, FilesystemHttpReader, LocalOnlyReader, Locator
from rashid.model import Report
from rashid.runner import validate
from tests.conftest import VALID_MULTIHASH, CatalogBuilder

pytestmark = pytest.mark.unit

_REMOTE_HREF = "https://data.example.org/elevation.tif"
_REMOTE_BYTES = bytes(range(256)) * 8
_LOCAL_HREF = "./items.parquet"
_LOCAL_BYTES = b"PAR1" + b"y" * 200_000  # spans several 64 KiB chunks


def _mirror_assets() -> dict[str, Any]:
    """One asset in the tree, one on another host: a mirror in miniature."""
    return {
        "data": {
            "href": _LOCAL_HREF,
            "type": "application/vnd.apache.parquet",
            "roles": ["data"],
            "file:size": len(_LOCAL_BYTES),
            "file:checksum": VALID_MULTIHASH,
        },
        "elevation": {
            "href": _REMOTE_HREF,
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "roles": ["data"],
            "file:size": len(_REMOTE_BYTES),
            "file:checksum": VALID_MULTIHASH,
        },
    }


def _mirror_graph(catalog: CatalogBuilder) -> tuple[CatalogGraph, Node]:
    catalog.collection("roads").item("seg1", assets=_mirror_assets())
    graph = CatalogGraph.load(catalog.write())
    item = next(node for node in graph.iter("item"))
    (item.abs_path.parent / "items.parquet").write_bytes(_LOCAL_BYTES)
    return graph, item


def _streaming_validator(consumed: dict[str, int | None]) -> Validator:
    """Stream every asset the reader will hand over, recording the byte count.

    ``None`` records an href the reader refused, which is what distinguishes a
    skipped asset from an empty one.
    """

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        if node.kind != "item":
            return []  # the builder's collection assets are placeholders, not the mirror
        for asset in node.data.get("assets", {}).values():
            href = asset["href"]
            stream = reader.stream(node, href)
            consumed[href] = None if stream is None else sum(len(chunk) for chunk in stream)
        return []

    return check


def _stub_server(payload: bytes) -> Any:
    class _Response:
        def __init__(self) -> None:
            self._pos = 0
            self.status = 200
            self.headers = {"Content-Length": str(len(payload))}

        def read(self, size: int = -1) -> bytes:
            chunk = payload[self._pos :] if size < 0 else payload[self._pos : self._pos + size]
            self._pos += len(chunk)
            return chunk

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: Any, **_kwargs: Any) -> _Response:
        return _Response()

    return fake_urlopen


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any outgoing request fail the test rather than merely be slow."""

    def refuse(request: Any, **_kwargs: Any) -> None:
        raise AssertionError(f"the reader reached the network: {request.full_url}")

    monkeypatch.setattr(reader_mod, "urlopen", refuse)


# --- LocalOnlyReader --------------------------------------------------------


def test_local_only_reader_resolves_assets_in_the_tree(catalog: CatalogBuilder) -> None:
    graph, item = _mirror_graph(catalog)

    located = LocalOnlyReader(graph).locate(item, _LOCAL_HREF)

    assert located == FilesystemHttpReader(graph).locate(item, _LOCAL_HREF)
    assert located == Locator(is_remote=False, source=str(item.abs_path.parent / "items.parquet"))


def test_local_only_reader_streams_local_bytes(catalog: CatalogBuilder) -> None:
    graph, item = _mirror_graph(catalog)

    stream = LocalOnlyReader(graph).stream(item, _LOCAL_HREF)

    assert stream is not None
    assert b"".join(stream) == _LOCAL_BYTES


def test_local_only_reader_drops_remote_hrefs(catalog: CatalogBuilder) -> None:
    graph, item = _mirror_graph(catalog)
    # The default reader locates it, so None here is the scope, not the href.
    assert FilesystemHttpReader(graph).locate(item, _REMOTE_HREF) is not None

    reader = LocalOnlyReader(graph)

    assert reader.locate(item, _REMOTE_HREF) is None
    assert reader.stream(item, _REMOTE_HREF) is None


@pytest.mark.parametrize(
    "href", ["s3://bucket/x.parquet", "http://insecure/x.parquet", "", "../escape.parquet"]
)
def test_local_only_reader_refuses_what_the_default_refuses(
    catalog: CatalogBuilder, href: str
) -> None:
    graph, item = _mirror_graph(catalog)
    reader = LocalOnlyReader(graph)

    assert reader.locate(item, href) is None
    assert reader.stream(item, href) is None


def test_local_only_reader_is_an_asset_reader(catalog: CatalogBuilder) -> None:
    """The protocol is what the validators are typed against."""
    graph, _item = _mirror_graph(catalog)
    reader: AssetReader = LocalOnlyReader(graph)
    assert reader is not None


# --- validate_data(reader_factory=...) --------------------------------------


def test_local_scope_consumes_zero_remote_bytes(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _item = _mirror_graph(catalog)
    _no_network(monkeypatch)
    consumed: dict[str, int | None] = {}

    findings = validate_data(graph, _streaming_validator(consumed), reader_factory=LocalOnlyReader)

    assert findings == []
    assert consumed == {_LOCAL_HREF: len(_LOCAL_BYTES), _REMOTE_HREF: None}


def test_default_scope_still_reads_remote_assets(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _item = _mirror_graph(catalog)
    monkeypatch.setattr(reader_mod, "urlopen", _stub_server(_REMOTE_BYTES))
    consumed: dict[str, int | None] = {}

    validate_data(graph, _streaming_validator(consumed))

    assert consumed == {_LOCAL_HREF: len(_LOCAL_BYTES), _REMOTE_HREF: len(_REMOTE_BYTES)}


def test_explicit_default_factory_matches_the_default(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, _item = _mirror_graph(catalog)
    monkeypatch.setattr(reader_mod, "urlopen", _stub_server(_REMOTE_BYTES))
    passed: dict[str, int | None] = {}
    omitted: dict[str, int | None] = {}

    validate_data(graph, _streaming_validator(passed), reader_factory=FilesystemHttpReader)
    validate_data(graph, _streaming_validator(omitted))

    assert passed == omitted


# --- runner threading -------------------------------------------------------


def _reader_recorder(seen: list[str]) -> Validator:
    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        seen.append(type(reader).__name__)
        return []

    return check


def test_runner_threads_the_factory_through(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    seen: list[str] = []

    validate(
        root,
        structural=False,
        data_validator=_reader_recorder(seen),
        data_reader_factory=LocalOnlyReader,
    )

    assert set(seen) == {"LocalOnlyReader"}


def test_runner_defaults_to_the_filesystem_http_reader(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    seen: list[str] = []

    validate(root, structural=False, data_validator=_reader_recorder(seen))

    assert set(seen) == {"FilesystemHttpReader"}


# --- the CLI option ---------------------------------------------------------


def _capture_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record what the CLI asks the runner for, without running a pass."""
    captured: dict[str, Any] = {}

    def fake_validate(catalog_path: Path | str, **kwargs: Any) -> Report:
        captured.update(kwargs)
        return Report(findings=[])

    monkeypatch.setattr(cli_mod, "validate", fake_validate)
    return captured


def test_cli_local_scope_asks_for_the_local_only_reader(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    captured = _capture_kwargs(monkeypatch)

    result = CliRunner().invoke(main, ["check", "--data-scope", "local", str(root)])

    assert result.exit_code == 0
    assert captured["data_reader_factory"] is LocalOnlyReader
    assert captured["data"] is True


def test_cli_default_scope_changes_nothing(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog.collection("roads")
    root = catalog.write()
    captured = _capture_kwargs(monkeypatch)

    CliRunner().invoke(main, ["check", str(root)])
    default = dict(captured)
    captured.clear()
    CliRunner().invoke(main, ["check", "--data-scope", "all", str(root)])

    assert captured == default
    assert default["data_reader_factory"] is None


def test_cli_local_scope_conflicts_with_no_data(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    result = CliRunner().invoke(main, ["check", "--data-scope", "local", "--no-data", str(root)])

    assert result.exit_code == 2
    assert "opposite things" in result.output


def test_cli_rejects_an_unknown_scope(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()

    result = CliRunner().invoke(main, ["check", "--data-scope", "mirror", str(root)])

    assert result.exit_code == 2
