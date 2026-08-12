"""Tests for the rashid.api surface.

These tests check what the module exports and that each export still
resolves to its implementation. The behaviour of each helper is tested in
the module that defines it.
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import timedelta
from pathlib import Path, PurePosixPath

import pytest

from rashid import api
from rashid.catalog import Node

pytestmark = pytest.mark.unit

# Every exported name, paired with the module that defines it. This catches
# a re-export that no longer resolves to its implementation.
EXPORTS = {
    "SHA2_256": "rashid._multihash",
    "SPDX_LICENSE_IDS": "rashid._spdx",
    "STRUCTURAL_RELS": "rashid.rules._common",
    "canonical_spdx_id": "rashid._spdx",
    "decode_multihash": "rashid._multihash",
    "encode_multihash": "rashid._multihash",
    "has_cog": "rashid.rules.item_mirror",
    "is_cog_media_type": "rashid.rules._common",
    "is_well_formed_multihash": "rashid._multihash",
    "links_of": "rashid.rules._common",
    "parse_rfc3339": "rashid.rules._common",
    "roles_of": "rashid.rules._common",
}


def _node(data: dict[str, object]) -> Node:
    return Node(
        path=PurePosixPath("collection.json"),
        abs_path=Path("/tmp/collection.json"),
        kind="collection",
        id="test",
        data=data,
    )


def test_all_lists_every_documented_export() -> None:
    assert sorted(api.__all__) == sorted(EXPORTS)


@pytest.mark.parametrize("name", sorted(EXPORTS))
def test_export_is_the_defining_module_s_object(name: str) -> None:
    source = importlib.import_module(EXPORTS[name])
    assert getattr(api, name) == getattr(source, name)


def test_structural_rels_covers_the_navigable_tree() -> None:
    assert set(api.STRUCTURAL_RELS) == {"root", "parent", "child", "item", "collection"}


def test_cog_media_type_needs_the_profile_parameter() -> None:
    assert api.is_cog_media_type("image/tiff; application=geotiff; profile=cloud-optimized")


def test_plain_geotiff_is_not_a_cog() -> None:
    # the bug this export exists to prevent, matching the prefix alone
    assert not api.is_cog_media_type("image/tiff; application=geotiff")


def test_cog_media_type_ignores_case_and_surrounding_space() -> None:
    assert api.is_cog_media_type("  IMAGE/TIFF; PROFILE=CLOUD-OPTIMIZED; APPLICATION=GEOTIFF  ")


def test_cog_media_type_needs_the_geotiff_parameter() -> None:
    assert not api.is_cog_media_type("image/tiff; profile=cloud-optimized")


def test_profile_on_a_non_tiff_type_is_not_a_cog() -> None:
    assert not api.is_cog_media_type("image/png; profile=cloud-optimized")


@pytest.mark.parametrize("value", [None, 42, ["image/tiff; profile=cloud-optimized"]])
def test_cog_media_type_tolerates_non_strings(value: object) -> None:
    assert not api.is_cog_media_type(value)


def test_encoded_sha256_carries_the_expected_prefix() -> None:
    digest = hashlib.sha256(b"portolan").digest()
    assert api.encode_multihash(api.SHA2_256, digest) == "1220" + digest.hex()


def test_encode_round_trips_through_decode() -> None:
    digest = hashlib.sha512(b"portolan").digest()
    decoded = api.decode_multihash(api.encode_multihash(0x13, digest))
    assert decoded == (0x13, digest)


def test_encoded_multihash_is_well_formed() -> None:
    encoded = api.encode_multihash(api.SHA2_256, hashlib.sha256(b"portolan").digest())
    assert api.is_well_formed_multihash(encoded)


def test_multi_byte_varints_round_trip() -> None:
    # a code above 0x7f spills into a second varint byte on both sides
    digest = b"\xab" * 200
    assert api.decode_multihash(api.encode_multihash(0x1053, digest)) == (0x1053, digest)


def test_encoding_an_empty_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="digest is empty"):
        api.encode_multihash(api.SHA2_256, b"")


def test_encoding_a_negative_code_is_refused() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        api.encode_multihash(-1, b"\xab")


def test_parse_rfc3339_reads_an_offset_aware_timestamp() -> None:
    parsed = api.parse_rfc3339("2026-07-28T12:00:00Z")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("value", [None, 20260728, "2026-07-28T12:00:00", "not a date"])
def test_parse_rfc3339_returns_none_for_anything_else(value: object) -> None:
    assert api.parse_rfc3339(value) is None


def test_has_cog_finds_a_cog_asset() -> None:
    node = _node(
        {
            "assets": {
                "thumb": {"type": "image/png"},
                "scene": {"type": "image/tiff; application=geotiff; profile=cloud-optimized"},
            }
        }
    )
    assert api.has_cog(node)


def test_has_cog_rejects_a_collection_of_plain_geotiffs() -> None:
    node = _node({"assets": {"source": {"type": "image/tiff; application=geotiff"}}})
    assert not api.has_cog(node)


def test_has_cog_tolerates_a_missing_assets_field() -> None:
    assert not api.has_cog(_node({}))


def test_canonical_spdx_id_returns_the_official_spelling() -> None:
    assert api.canonical_spdx_id("apache-2.0") == "Apache-2.0"


def test_canonical_spdx_id_passes_an_exact_identifier_through() -> None:
    assert api.canonical_spdx_id("CC-BY-4.0") == "CC-BY-4.0"


def test_canonical_spdx_id_returns_none_for_a_non_identifier() -> None:
    # the near miss PTL-LIC-001 cannot help with: a name, not a spelling
    assert api.canonical_spdx_id("Apache 2.0") is None


@pytest.mark.parametrize("value", [None, 42, ["MIT"]])
def test_canonical_spdx_id_tolerates_non_strings(value: object) -> None:
    assert api.canonical_spdx_id(value) is None


def test_the_exported_list_carries_identifiers_beyond_the_popular_ones() -> None:
    # the downstream case in portolan-cli#727: a real identifier that a
    # hand-maintained shortlist omits
    assert "EUPL-1.2" in api.SPDX_LICENSE_IDS
    assert "OGL-UK-3.0" in api.SPDX_LICENSE_IDS


def test_the_exported_list_holds_no_licenseref_entries() -> None:
    # downstream tools gate on this: LicenseRef-* is an SPDX expression
    # construct, not an identifier, so PTL-LIC-001 rejects every one
    assert [i for i in api.SPDX_LICENSE_IDS if i.casefold().startswith("licenseref")] == []


def test_the_exported_list_excludes_the_stac_escape_hatch() -> None:
    # "other" is a STAC value, not SPDX; a caller has to allow it separately
    assert "other" not in api.SPDX_LICENSE_IDS
