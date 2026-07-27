from __future__ import annotations

import pytest

from rashid._multihash import is_well_formed_multihash
from tests.conftest import VALID_MULTIHASH

pytestmark = pytest.mark.unit


def test_valid_sha256_multihash() -> None:
    assert is_well_formed_multihash(VALID_MULTIHASH)


def test_valid_sha512_multihash() -> None:
    # varint(0x13) + varint(0x40) + 64 bytes
    assert is_well_formed_multihash("1340" + "ab" * 64)


def test_raw_sha256_hex_is_rejected() -> None:
    # a bare digest: first byte 0xe3 reads as a varint code, but the
    # remaining bytes cannot match the declared digest length
    assert not is_well_formed_multihash(VALID_MULTIHASH[4:])


def test_truncated_digest_is_rejected() -> None:
    assert not is_well_formed_multihash("1220" + "ab" * 31)


def test_excess_bytes_are_rejected() -> None:
    assert not is_well_formed_multihash("1220" + "ab" * 33)


def test_odd_length_hex_is_rejected() -> None:
    assert not is_well_formed_multihash("1220abc")


def test_non_hex_is_rejected() -> None:
    assert not is_well_formed_multihash("zz20" + "ab" * 32)


def test_empty_and_non_string_are_rejected() -> None:
    assert not is_well_formed_multihash("")
    assert not is_well_formed_multihash(None)
    assert not is_well_formed_multihash(1234)


def test_zero_length_digest_is_rejected() -> None:
    assert not is_well_formed_multihash("1200")
