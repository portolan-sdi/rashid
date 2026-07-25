"""Live pass prober against a real server — networked, self-skipping.

Exercises the default urllib prober's three request shapes end to end against
a small immutable blob (a pinned commit of this repo's README on GitHub, which
serves ranges). It asserts the transport facts the prober must extract —
status, lowercased headers — NOT GitHub's CORS policy: that is the hosting
provider's configuration, not this codebase's behavior, and pinning it would
make the test flake on their config changes.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.request import urlopen

import pytest

from reis.live import ProbeResponse, _UrllibProber

pytestmark = [pytest.mark.integration, pytest.mark.network]

# An immutable blob: this repo's README at a merged commit. Public, range-capable.
_URL = "https://raw.githubusercontent.com/portolan-sdi/reis/312ff6e/README.md"


def _require_reachable(url: str) -> None:
    try:
        with urlopen(url, timeout=30) as response:  # noqa: S310 - literal https URL
            if response.status != 200:
                pytest.skip(f"{url} returned {response.status}")
    except OSError as exc:  # pragma: no cover - network dependent; URLError is an OSError
        pytest.skip(f"cannot reach {url}: {exc}")


def _probe(call: Callable[[str], ProbeResponse], url: str) -> ProbeResponse:
    """Run one probe, skipping when the connection drops mid-test."""
    try:
        return call(url)
    except OSError as exc:  # pragma: no cover - network dependent
        pytest.skip(f"probe of {url} failed in transport: {exc}")


def test_prober_reads_range_and_head() -> None:
    _require_reachable(_URL)
    prober = _UrllibProber()

    ranged = _probe(prober.get_range, _URL)
    assert ranged.status == 206
    assert (ranged.header("accept-ranges") or "").lower() == "bytes"
    assert ranged.header("content-range") is not None

    headed = _probe(prober.head, _URL)
    length = headed.header("content-length")
    assert length is not None and length.isdigit() and int(length) > 0


def test_prober_survives_a_preflight() -> None:
    # The preflight must come back as a ProbeResponse whatever the server's
    # CORS verdict is — HTTPError (405, 403) folds into status, never raises.
    _require_reachable(_URL)
    response = _probe(_UrllibProber().preflight, _URL)
    assert isinstance(response.status, int)


def test_prober_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="non-https"):
        _UrllibProber().get_range("http://example.com/data.parquet")
