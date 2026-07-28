"""Multihash well-formedness checking.

A multihash is ``varint(code) + varint(digest length) + digest``, hex-encoded
(https://github.com/multiformats/multihash). This module only checks that a
string decodes as one; verifying the digest against file bytes is a data-pass
job, so no hashing happens here and no dependency is needed.
"""

from __future__ import annotations

# Hash-function code from the multiformats table. Portolan tooling writes
# sha2-256. The decoder accepts whatever code a producer used.
SHA2_256 = 0x12


def _write_varint(value: int) -> bytes:
    """Encode a non-negative int as an unsigned varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    """Decode an unsigned varint at ``offset``; return (value, next_offset)."""
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        value |= (byte & 0x7F) << shift
        offset += 1
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:  # implausibly large varint; corrupt input
            return None
    return None  # ran out of bytes mid-varint


def decode_multihash(value: object) -> tuple[int, bytes] | None:
    """Decode a hex multihash into ``(hash-function code, digest bytes)``.

    Returns None when ``value`` is not a hex string that decodes as a complete
    multihash. The code names the hash function (e.g. ``0x12`` = sha2-256); the
    data pass maps it to a hashlib algorithm to verify the digest against the
    asset's bytes.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    decoded = _read_varint(raw, 0)
    if decoded is None:
        return None
    code, offset = decoded
    decoded = _read_varint(raw, offset)
    if decoded is None:
        return None
    length, offset = decoded
    digest = raw[offset:]
    if length <= 0 or len(digest) != length:
        return None
    return code, digest


def encode_multihash(code: int, digest: bytes) -> str:
    """Encode a hash-function code and digest as a hex multihash.

    This is the inverse of :func:`decode_multihash`, for tools that write
    ``file:checksum`` rather than check it. The usual call passes
    :data:`SHA2_256` and the output of ``hashlib.sha256(...).digest()``.

    Raises:
        ValueError: When ``code`` is negative or ``digest`` is empty. Neither
            decodes again, and encoding one would write an invalid checksum.
    """
    if code < 0:
        raise ValueError(f"hash-function code must be non-negative, got {code}")
    if not digest:
        raise ValueError("digest is empty")
    return (_write_varint(code) + _write_varint(len(digest)) + digest).hex()


def is_well_formed_multihash(value: object) -> bool:
    """True when ``value`` is a hex string decoding as a complete multihash."""
    return decode_multihash(value) is not None
