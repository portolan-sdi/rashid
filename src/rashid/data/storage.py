"""Asset byte verification and format probes."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

from rashid import _trace
from rashid._multihash import decode_multihash
from rashid.catalog import Node
from rashid.data import DataDefect
from rashid.data.reader import AssetReader
from rashid.model import Severity
from rashid.registry import (
    DAT_CHECKSUM,
    DAT_FORMAT,
    DAT_SIZE,
)
from rashid.rules._common import (
    is_tiff_media_type,
)

# Multihash function code -> hashlib algorithm name.
_HASH_ALGOS = {
    0x11: "sha1",
    0x12: "sha256",
    0x13: "sha512",
    0x14: "sha3_512",
    0x15: "sha3_384",
    0x16: "sha3_256",
    0x17: "sha3_224",
}

# Recognized formats: (magic-byte probe, media-type marker).
_PMTILES_MAGIC = b"PMTiles"
_HEAD_BYTES = 16  # enough for every magic-number probe below


def _check_bytes(
    key: str,
    *,
    asset: dict[str, Any],
    expected: str | None,
    node: Node,
    reader: AssetReader,
) -> list[DataDefect]:
    """Verify checksum, size, and format magic, reading only what they need.

    The three findings do not cost the same. A recomputed digest and a byte
    counter each need every byte; the format magic needs ``_HEAD_BYTES``. So the
    metadata decides how much of the object is read, and an asset that declares
    none of the three is never fetched — for a catalog of remote COGs that is
    the difference between a full download per asset and no request at all
    (#86). What is reported does not change: the same three verifications run on
    the same bytes they always looked at.

    Two absences are what they look like elsewhere in this module. A
    ``file:checksum`` naming a hash function rashid cannot compute yields no
    hasher, and :func:`_verify_checksum` answers it with an INFO drawn from the
    declaration alone, so those bytes would settle nothing. A ``file:size``
    that is not an integer is absent to :func:`_verify_size`, and the guard here
    is that same test so the two cannot disagree.

    The stream is still requested up front, and ``None`` still means silence:
    both readers build it lazily (``_http_stream`` is a generator, so no request
    leaves until it is iterated), which makes asking for one the cheapest
    fetchability test available through the ``AssetReader`` protocol.
    """
    stream = reader.stream(node, asset["href"])
    if stream is None:
        return []  # not fetchable; metadata pass owns missing/foreign hrefs

    decoded = decode_multihash(asset.get("file:checksum"))
    algo, hasher = _hasher_for(decoded)
    declared_size = asset.get("file:size")
    sized = isinstance(declared_size, int) and not isinstance(declared_size, bool)

    if hasher is None and not sized:
        if expected is None:
            _close(stream)
            return _verify_checksum(key, decoded, algo, hasher)
        try:
            head = _read_head(stream)
        except OSError as exc:
            return [_unread(key, exc)]
        return _verify_checksum(key, decoded, algo, hasher) + _verify_format(key, expected, head)

    try:
        head, count = _consume(stream, hasher)
    except OSError as exc:
        return [_unread(key, exc)]

    defects: list[DataDefect] = []
    defects.extend(_verify_checksum(key, decoded, algo, hasher))
    defects.extend(_verify_size(key, declared_size, count))
    defects.extend(_verify_format(key, expected, head))
    return defects


def _hasher_for(decoded: tuple[int, bytes] | None) -> tuple[str | None, Any]:
    """The hashlib name and hasher for a decoded multihash, or ``(None, None)``.

    A code outside ``_HASH_ALGOS`` leaves both None: the digest cannot be
    recomputed, and :func:`_verify_checksum` reports that from the code alone.
    """
    if decoded is None:
        return None, None
    algo = _HASH_ALGOS.get(decoded[0])
    return algo, hashlib.new(algo) if algo is not None else None


def _consume(stream: Iterator[bytes], hasher: Any) -> tuple[bytes, int]:
    """Read the whole object, returning its head and its byte count."""
    head = b""
    count = 0
    for chunk in stream:
        count += len(chunk)
        if len(head) < _HEAD_BYTES:
            head += chunk[: _HEAD_BYTES - len(head)]
        if hasher is not None:
            hasher.update(chunk)
    _trace.count("data.bytes_read", count)
    return head, count


def _read_head(stream: Iterator[bytes]) -> bytes:
    """The object's first ``_HEAD_BYTES``, then stop pulling.

    Every probe in :func:`_detect_format` reads within that prefix, so the rest
    of the object cannot change the answer. A short object simply ends first.
    """
    head = b""
    try:
        for chunk in stream:
            head += chunk[: _HEAD_BYTES - len(head)]
            if len(head) >= _HEAD_BYTES:
                break
    finally:
        _close(stream)
    return head


def _close(stream: Iterator[bytes]) -> None:
    """Release a stream this check is done with, rather than leave it to the collector.

    ``_http_stream`` is a generator holding an open obstore response, so a
    stream dropped part-way keeps a connection until it is reclaimed.
    ``AssetReader.stream`` promises only an ``Iterator[bytes]``, which need not
    be closeable, hence the guard.
    """
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _unread(key: str, exc: OSError) -> DataDefect:
    """A read that failed is unverified, not nonconformant."""
    return DataDefect(
        DAT_CHECKSUM,
        Severity.INFO,
        f"asset '{key}' bytes could not be read ({exc}); not verified",
        key,
    )


def _verify_checksum(
    key: str,
    decoded: tuple[int, bytes] | None,
    algo: str | None,
    hasher: Any,
) -> list[DataDefect]:
    if decoded is None:
        return []  # absent or malformed: PTL-AST-003/004 own that
    if algo is None:
        code = decoded[0]
        return [
            DataDefect(
                DAT_CHECKSUM,
                Severity.INFO,
                f"asset '{key}' file:checksum uses hash function 0x{code:x}, "
                "which rashid cannot compute; not verified",
                key,
                "file:checksum",
            )
        ]
    if hasher.digest() != decoded[1]:
        return [
            DataDefect(
                DAT_CHECKSUM,
                Severity.ERROR,
                f"asset '{key}' file:checksum does not match the bytes "
                f"(declared {hasher.name} digest differs from recomputed)",
                key,
                "file:checksum",
                expected=hasher.digest().hex(),
                actual=decoded[1].hex(),
            )
        ]
    return []


def _verify_size(key: str, declared: Any, count: int) -> list[DataDefect]:
    if isinstance(declared, bool) or not isinstance(declared, int):
        return []  # absent or non-integer: PTL-AST-003 owns that
    if declared != count:
        return [
            DataDefect(
                DAT_SIZE,
                Severity.ERROR,
                f"asset '{key}' file:size is {declared} but the bytes are {count}",
                key,
                "file:size",
                expected=count,
                actual=declared,
            )
        ]
    return []


def _verify_format(key: str, expected: str | None, head: bytes) -> list[DataDefect]:
    if expected is None:
        return []
    actual = _detect_format(head)
    if actual is None or actual == expected:
        return []
    return [
        DataDefect(
            DAT_FORMAT,
            Severity.ERROR,
            f"asset '{key}' declares {expected} but its bytes are {actual}",
            key,
            "type",
        )
    ]


# --- format probing --------------------------------------------------------


def _expected_format(media_type: str) -> str | None:
    lowered = media_type.lower()
    if "parquet" in lowered:
        return "parquet"
    if is_tiff_media_type(media_type, policy="prefix"):
        return "tiff"
    if "pmtiles" in lowered:
        return "pmtiles"
    if lowered.startswith("image/png"):
        return "png"
    if lowered.startswith(("image/jpeg", "image/jpg")):
        return "jpeg"
    return None


def _detect_format(head: bytes) -> str | None:
    if head[:4] == b"PAR1":
        return "parquet"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[: len(_PMTILES_MAGIC)] == _PMTILES_MAGIC:
        return "pmtiles"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return None
