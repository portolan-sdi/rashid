"""How much of an asset the byte checks actually read.

``_check_bytes`` derives three findings from one pass over the object: the
checksum needs every byte, ``file:size`` needs every byte, and the format magic
needs the first ``_HEAD_BYTES``. When none of the three can be produced, the
object must not be touched at all — a remote COG on someone else's host is a
full download per asset otherwise (#86). These tests assert on bytes handed out,
not only on findings, because the findings were already correct before.

The reader is a fake serving in-memory bytes, so nothing here reaches the
network or the geospatial stack, even though importing ``rashid.data.checks``
pulls that stack in.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import rashid.data.checks as checks
from rashid.catalog import Node
from rashid.data import DAT_CHECKSUM, DAT_FORMAT, DAT_SIZE, DataDefect
from rashid.data.reader import Locator
from rashid.model import Severity

pytestmark = pytest.mark.unit

_HREF = "./data.parquet"
_PARQUET = "application/vnd.apache.parquet"
_MIB = 1 << 20
# A stand-in for a real asset: parquet magic, then bulk no check needs.
_PAYLOAD = b"PAR1" + b"\x00" * (4 * _MIB - 4)


def _multihash(payload: bytes, code: str = "12") -> str:
    return code + "20" + hashlib.sha256(payload).hexdigest()


class _CountingStream:
    """An iterator over one payload that records what it handed out.

    A class rather than a generator so ``closed`` records an explicit
    ``close()`` call and not the collector reclaiming an abandoned generator.
    """

    def __init__(self, payload: bytes, chunk: int) -> None:
        self._payload = payload
        self._chunk = chunk
        self._pos = 0
        self.consumed = 0
        self.closed = False

    def __iter__(self) -> _CountingStream:
        return self

    def __next__(self) -> bytes:
        if self._pos >= len(self._payload):
            raise StopIteration
        piece = self._payload[self._pos : self._pos + self._chunk]
        self._pos += len(piece)
        self.consumed += len(piece)
        return piece

    def close(self) -> None:
        self.closed = True


class _ExplodingStream:
    """A stream that fails on its first chunk, as a reset connection does."""

    def __init__(self) -> None:
        self.closed = False

    def __iter__(self) -> _ExplodingStream:
        return self

    def __next__(self) -> bytes:
        raise OSError("connection reset")

    def close(self) -> None:
        self.closed = True


class _CountingReader:
    """Serves one href's bytes and keeps the stream it handed out.

    ``locate`` returns None so ``check_node`` stops after the byte checks: the
    format-specific checks read the same fake bytes through a real path and
    would drown the byte count this file is about.
    """

    def __init__(
        self,
        payload: bytes = _PAYLOAD,
        *,
        chunk: int = 8,
        fetchable: bool = True,
        stream: Any = None,
    ) -> None:
        self.stream_handed: Any = stream if stream is not None else _CountingStream(payload, chunk)
        self._fetchable = fetchable

    @property
    def consumed(self) -> int:
        count = getattr(self.stream_handed, "consumed", 0)
        return int(count)

    @property
    def closed(self) -> bool:
        return bool(self.stream_handed.closed)

    def locate(self, node: Node, href: str) -> Locator | None:
        return None

    def stream(self, node: Node, href: str) -> Any:
        if not self._fetchable or href != _HREF:
            return None
        return self.stream_handed


def _item(asset: dict[str, Any]) -> Node:
    return Node(
        path=PurePosixPath("roads/seg1/seg1.json"),
        abs_path=Path("/nowhere/seg1.json"),
        kind="item",
        id="seg1",
        data={"type": "Feature", "assets": {"data": asset}},
    )


def _asset(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"href": _HREF, "roles": ["data"]}
    base.update(over)
    return base


def _run(asset: dict[str, Any], reader: _CountingReader) -> list[DataDefect]:
    return checks.check_node(_item(asset), reader)


def test_nothing_to_verify_reads_no_bytes() -> None:
    """No checksum, no size, no type: the object is never touched."""
    reader = _CountingReader()

    defects = _run(_asset(), reader)

    assert defects == []
    assert reader.consumed == 0
    assert reader.closed is True


def test_format_only_reads_just_the_head() -> None:
    """Only ``type`` is verifiable, and 16 bytes settle it."""
    reader = _CountingReader(chunk=8)

    defects = _run(_asset(type=_PARQUET), reader)

    assert defects == []
    assert reader.consumed == checks._HEAD_BYTES
    assert reader.closed is True


def test_format_mismatch_still_found_from_the_head() -> None:
    """The early exit narrows the read, not the finding."""
    reader = _CountingReader(chunk=8)

    defects = _run(_asset(type="application/vnd.pmtiles"), reader)

    assert [d.rule_id for d in defects] == [DAT_FORMAT]
    assert "pmtiles" in defects[0].message and "parquet" in defects[0].message
    assert reader.consumed == checks._HEAD_BYTES


def test_checksum_still_reads_every_byte() -> None:
    reader = _CountingReader(chunk=64 * 1024)
    asset = _asset(type=_PARQUET, **{"file:checksum": _multihash(_PAYLOAD)})

    defects = _run(asset, reader)

    assert defects == []
    assert reader.consumed == len(_PAYLOAD)


def test_size_still_reads_every_byte() -> None:
    reader = _CountingReader(chunk=64 * 1024)

    defects = _run(_asset(**{"file:size": 999}), reader)

    assert [d.rule_id for d in defects] == [DAT_SIZE]
    assert defects[0].expected == len(_PAYLOAD)
    assert reader.consumed == len(_PAYLOAD)


def test_matching_checksum_and_size_verify_over_the_full_stream() -> None:
    reader = _CountingReader(chunk=64 * 1024)
    asset = _asset(
        type=_PARQUET,
        **{"file:size": len(_PAYLOAD), "file:checksum": _multihash(_PAYLOAD)},
    )

    assert _run(asset, reader) == []
    assert reader.consumed == len(_PAYLOAD)


def test_unsupported_hash_function_is_info_without_reading() -> None:
    """0x18 (keccak-256) decodes but rashid cannot compute it, so no bytes help."""
    reader = _CountingReader()
    asset = _asset(**{"file:checksum": "1820" + "00" * 32})

    defects = _run(asset, reader)

    assert [d.rule_id for d in defects] == [DAT_CHECKSUM]
    assert defects[0].severity is Severity.INFO
    assert reader.consumed == 0
    assert reader.closed is True


def test_malformed_checksum_reads_nothing() -> None:
    """PTL-AST-004 owns an undecodable multihash, so the bytes are moot."""
    reader = _CountingReader()

    defects = _run(_asset(**{"file:checksum": "not-a-multihash"}), reader)

    assert defects == []
    assert reader.consumed == 0


@pytest.mark.parametrize("declared", [True, "1024", 10.0, None])
def test_non_integer_size_reads_nothing(declared: Any) -> None:
    """``_verify_size`` ignores a non-integer, bools included, so nothing needs the bytes."""
    reader = _CountingReader()

    defects = _run(_asset(**{"file:size": declared}), reader)

    assert defects == []
    assert reader.consumed == 0


def test_non_fetchable_href_stays_silent() -> None:
    """Unchanged: the metadata pass owns a missing or foreign href."""
    reader = _CountingReader(fetchable=False)
    asset = _asset(type=_PARQUET, **{"file:checksum": "1820" + "00" * 32, "file:size": 12})

    assert _run(asset, reader) == []
    assert reader.consumed == 0


def test_unreadable_full_stream_is_info() -> None:
    reader = _CountingReader(stream=_ExplodingStream())
    asset = _asset(**{"file:checksum": _multihash(_PAYLOAD)})

    defects = _run(asset, reader)

    assert [d.rule_id for d in defects] == [DAT_CHECKSUM]
    assert defects[0].severity is Severity.INFO
    assert "could not be read" in defects[0].message


def test_unreadable_head_is_info() -> None:
    """The head-only path keeps the OSError handling of the full path."""
    reader = _CountingReader(stream=_ExplodingStream())

    defects = _run(_asset(type=_PARQUET), reader)

    assert [d.rule_id for d in defects] == [DAT_CHECKSUM]
    assert defects[0].severity is Severity.INFO


def test_stream_without_close_is_tolerated() -> None:
    """``AssetReader.stream`` promises an iterator, which need not be closeable."""

    class _Bare:
        def locate(self, node: Node, href: str) -> Locator | None:
            return None

        def stream(self, node: Node, href: str) -> Any:
            return iter([b"PAR1"])

    assert checks.check_node(_item(_asset()), _Bare()) == []
