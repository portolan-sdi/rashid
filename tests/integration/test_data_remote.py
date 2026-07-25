"""Data pass over a real remote ``https`` asset — the only networked test.

Proves the remote fetch-and-hash path end to end against a live server: a small
immutable file (a pinned commit of this repo's README, served by GitHub with
range support). The expected checksum is computed from the fetch itself, so
nothing is pinned to content that could change; the test asserts the pass
verifies a correct checksum and flags a corrupted one. Marked ``network`` and
excluded from the default run; skips gracefully when the host is unreachable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import urlopen

import pytest

from reis.catalog import CatalogGraph
from reis.data import DAT_CHECKSUM, DAT_SIZE, validate_data
from reis.model import Severity
from tests.conftest import CatalogBuilder, skip_if_network_flaked

pytestmark = [pytest.mark.integration, pytest.mark.network]

# An immutable blob: this repo's README at a merged commit. Public, range-capable.
_URL = "https://raw.githubusercontent.com/portolan-sdi/reis/312ff6e/README.md"


def _fetch(url: str) -> bytes:
    try:
        with urlopen(url, timeout=30) as response:  # noqa: S310 - literal https URL
            if response.status != 200:
                pytest.skip(f"{url} returned {response.status}")
            return bytes(response.read())
    except OSError as exc:  # pragma: no cover - network dependent; URLError is an OSError
        pytest.skip(f"cannot reach {url}: {exc}")


def _multihash(payload: bytes) -> str:
    return "1220" + hashlib.sha256(payload).hexdigest()


def _catalog_with_remote_asset(root: Path, checksum: str, size: int) -> Path:
    cat = CatalogBuilder(root)
    cat.collection("docs").item(
        "readme",
        assets={
            "data": {
                "href": _URL,
                "type": "text/markdown",
                "roles": ["data"],
                "file:size": size,
                "file:checksum": checksum,
            }
        },
    )
    return cat.write()


def test_remote_checksum_and_size_verify(tmp_path: Path) -> None:
    payload = _fetch(_URL)
    root = _catalog_with_remote_asset(tmp_path / "catalog", _multihash(payload), len(payload))

    findings = validate_data(CatalogGraph.load(root))
    skip_if_network_flaked(f.message for f in findings)

    assert not any(f.rule_id in {DAT_CHECKSUM, DAT_SIZE} for f in findings), [
        f.message for f in findings
    ]


def test_remote_wrong_checksum_flags_dat_001(tmp_path: Path) -> None:
    payload = _fetch(_URL)
    wrong = _multihash(payload + b"tamper")
    root = _catalog_with_remote_asset(tmp_path / "catalog", wrong, len(payload))

    findings = validate_data(CatalogGraph.load(root))
    skip_if_network_flaked(f.message for f in findings)

    # An unread asset also reports DAT_CHECKSUM, at INFO; the mismatch is an error.
    assert [f.rule_id for f in findings if f.severity is Severity.ERROR] == [DAT_CHECKSUM]
