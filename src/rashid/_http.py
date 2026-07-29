"""What every outgoing HTTP request identifies itself as.

Two passes reach the network — the live prober and the data pass's byte
reader — and both must send the same agent, so the string lives here rather
than in either of them. Neither pass imports the other, and this module is
stdlib-only, so it stays importable from both without creating an edge
between the passes.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

_PROJECT_URL = "https://github.com/portolan-sdi/rashid"


@lru_cache(maxsize=1)
def user_agent() -> str:
    """The ``User-Agent`` every request this package sends must carry.

    urllib defaults to ``Python-urllib/3.x``, which edge providers block:
    Cloudflare answers it with a 403 and a 17-byte error page, and a pass then
    reads that page back as the asset's bytes or its ``Content-Length``,
    failing rules against a healthy host. Naming the validator, its version,
    and its homepage also makes the traffic legible to the operators whose
    servers are being read.

    Read at first use, not at import, so `import rashid` stays cheap.
    """
    try:
        release = version("rashid")
    except PackageNotFoundError:  # pragma: no cover - installed metadata is always present
        release = "unknown"
    return f"rashid/{release} (+{_PROJECT_URL})"
