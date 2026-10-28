"""What every outgoing HTTP request identifies itself as.

Four paths reach the network: the URL-mode crawl, the live prober, the data
pass's byte reader, and the schema pass fetching an unbundled profile schema.
All must send the same agent, so the string lives here rather than in any of
them. Most requests go out through :mod:`rashid._store`, over obstore or, for
what obstore cannot send, over urllib; the live prober's once-per-host probes
go out over urllib. None of the passes imports the others, and this module is
stdlib-only, so it stays importable from all of them without creating an edge
between the passes.

Raster reads go out through GDAL's own HTTP stack rather than obstore, so they
carry GDAL's agent, not this one. That agent defaults to ``GDAL/x.y.z``, which
is named rather than anonymous, so those reads do not hit the 403 described
below; the gap is one of consistency. Closing it means passing
``GDAL_HTTP_USERAGENT`` to every GDAL entry point in the data pass, the
``rasterio.Env`` blocks and ``cog_validate``'s ``config`` alike.
"""

from __future__ import annotations

from functools import lru_cache

_PROJECT_URL = "https://github.com/portolan-sdi/rashid"


@lru_cache(maxsize=1)
def user_agent() -> str:
    """The ``User-Agent`` every request this package sends must carry.

    urllib defaults to ``Python-urllib/3.x``, and edge providers block such agents:
    Cloudflare answers it with a 403 and a 17-byte error page, and a pass then
    reads that page back as the asset's bytes or its ``Content-Length``,
    failing rules against a healthy host. Naming the validator, its version,
    and its homepage also makes the traffic legible to the operators whose
    servers are being read.

    Read at first use, not at import, so `import rashid` stays cheap.
    ``importlib.metadata`` alone costs about 9 ms to import.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        release = version("rashid")
    except PackageNotFoundError:  # pragma: no cover - installed metadata is always present
        release = "unknown"
    return f"rashid/{release} (+{_PROJECT_URL})"
