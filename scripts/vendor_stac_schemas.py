"""Vendor the STAC core and extension JSON Schema closures into the package.

The structural pass validates every object against the STAC core schemas for
its ``type``; the extension pass validates it against the schemas of the STAC
extensions it declares. Both sets ship in the wheel
(``src/rashid/_schemas/``) so the passes run offline; this script is the only
way they get there. It crawls the transitive ``$ref`` closure from every root
and writes each file byte-identical to upstream — the ``$id`` normalization
that resolution needs happens at load time in :mod:`rashid._jsonschema`, never
on disk, so ``--check`` stays an honest diff against upstream.

The core roots are the three STAC 1.1.0 schemas, fixed here. The extension
roots are **not** listed here: they are read from
``src/rashid/_schemas/extension-registry.json``, which
``scripts/vendor_spec_fixtures.py`` syncs from the Portolan STAC profile. The
profile's extension registry is the single source of truth for which
extensions Portolan approves and which version of each it pins, so pinning a
new version in the spec vendors its schema on the next spec sync with no code
change here. That ordering matters: the fixture script must run before this
one, or this one reads the previous registry.

Usage::

    uv run python scripts/vendor_stac_schemas.py           # (re)write
    uv run python scripts/vendor_stac_schemas.py --check   # diff, exit 1 on drift

The spec-sync canary runs ``--check`` nightly: published schemas are immutable
per version in principle, but a silent upstream edit would otherwise change
verdicts between rashid releases.

Ported from portolan-cli's ``scripts/refresh_stac_schemas.py``.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit

STAC_VERSION = "1.1.0"
_STAC_HOST = "https://schemas.stacspec.org/"
_STAC_BASE = f"{_STAC_HOST}v{STAC_VERSION}/"
# item.json reaches outside the STAC host: its GeoJSON geometry model is a
# $ref to geojson.org. Extension schemas reach further still — PROJJSON on
# proj.org, and STAC 1.0.0 basics.json back on the STAC host. Every host here
# publishes immutable, versioned-in-practice schemas, so all are vendorable.
_GEOJSON_BASE = "https://geojson.org/schema/"
_ROOTS = (
    f"{_STAC_BASE}catalog-spec/json-schema/catalog.json",
    f"{_STAC_BASE}collection-spec/json-schema/collection.json",
    f"{_STAC_BASE}item-spec/json-schema/item.json",
)

# Hosts a transitive $ref may reach. A root on a host outside this set is
# allowed when the profile's registry names it (a spec decision, already
# reviewed); a *transitive* ref to an unknown host stays a hard error, because
# nobody approved it.
_FIXED_HOSTS = ("schemas.stacspec.org", "geojson.org", "proj.org")

# The Portolan profile schema is vendored under _schemas/portolan/ by
# scripts/vendor_spec_fixtures.py, and validated by the --schema pass. Skip it
# here so it is not vendored twice under two different paths.
_PROFILE_URI = re.compile(
    r"^https://schemas\.portolan-sdi\.org/portolan/v\d+\.\d+\.\d+/schema\.json$"
)

# proj.org rejects the bare urllib User-Agent with HTTP 403; every other host
# serves either way. Named so an operator reading their logs can find us.
_USER_AGENT = "rashid-schema-vendor (+https://www.portolan-sdi.org/)"

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "src" / "rashid" / "_schemas"
_GEOJSON_VENDOR_DIR = _SCHEMAS_DIR / "geojson"
_EXTENSION_VENDOR_DIR = _SCHEMAS_DIR / "extensions"
_REGISTRY_FILE = _SCHEMAS_DIR / "extension-registry.json"

_STAC_PATH = re.compile(r"^/v(?P<version>[^/]+)/(?P<rest>.+)$")


def extension_roots() -> tuple[str, ...]:
    """The extension schema URIs to vendor, read from the synced registry.

    The registry is a spec artifact. This function never filters on which
    extensions rashid "knows about" — every entry the profile pins is
    vendored, so a new extension needs no code change here.
    """
    if not _REGISTRY_FILE.exists():
        raise SystemExit(f"missing {_REGISTRY_FILE}; run scripts/vendor_spec_fixtures.py first")
    registry = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
    extensions = registry.get("extensions")
    if not isinstance(extensions, dict) or not extensions:
        raise SystemExit(f"no extensions in {_REGISTRY_FILE}")
    return tuple(
        uri for uri in extensions.values() if isinstance(uri, str) and not _PROFILE_URI.match(uri)
    )


def _allowed_hosts(roots: tuple[str, ...]) -> frozenset[str]:
    """Hosts the crawl may fetch from: the fixed set plus every root's host."""
    return frozenset(_FIXED_HOSTS) | {urlsplit(url).netloc for url in roots}


def _refs(node: object) -> list[str]:
    out: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, dict):
            ref = o.get("$ref")
            if isinstance(ref, str):
                out.append(ref)
            for value in o.values():
                walk(value)
        elif isinstance(o, list):
            for value in o:
                walk(value)

    walk(node)
    return out


def _fetch(url: str) -> dict[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        schema: dict[str, object] = json.loads(response.read().decode("utf-8"))
    return schema


def crawl() -> dict[str, dict[str, object]]:
    """Fetch the transitive $ref closure of every root, keyed by URL."""
    roots = _ROOTS + extension_roots()
    allowed = _allowed_hosts(roots)
    fetched: dict[str, dict[str, object]] = {}
    queue: collections.deque[str] = collections.deque(roots)
    while queue:
        url = queue.popleft()
        if url in fetched:
            continue
        if urlsplit(url).netloc not in allowed:
            raise SystemExit(f"refusing ref outside the vendorable hosts: {url}")
        fetched[url] = schema = _fetch(url)
        for ref in _refs(schema):
            absolute = urljoin(url, ref.split("#", 1)[0])
            if absolute.startswith("https://") and absolute not in fetched:
                queue.append(absolute)
    return fetched


def _dest(url: str) -> Path:
    """Map a schema URL to its vendored path.

    STAC and GeoJSON keep the layout they have always had, so re-running this
    script after the extension roots were added rewrites no existing file.
    Everything else lands under ``extensions/<host>/<path>``, which keeps the
    URL recoverable at load time and cannot collide across hosts.
    """
    if url.startswith(_GEOJSON_BASE):
        return _GEOJSON_VENDOR_DIR / url.removeprefix(_GEOJSON_BASE)
    split = urlsplit(url)
    if url.startswith(_STAC_HOST):
        match = _STAC_PATH.match(split.path)
        if match is None:
            raise SystemExit(f"unrecognised STAC schema URL: {url}")
        return _SCHEMAS_DIR / "stac" / match["version"] / match["rest"]
    return _EXTENSION_VENDOR_DIR / split.netloc / split.path.lstrip("/")


def write(fetched: dict[str, dict[str, object]]) -> None:
    for url, schema in fetched.items():
        dest = _dest(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


def check(fetched: dict[str, dict[str, object]]) -> int:
    status = 0
    for url, schema in fetched.items():
        dest = _dest(url)
        if not dest.exists():
            print(f"missing: {dest}")
            status = 1
            continue
        if json.loads(dest.read_text(encoding="utf-8")) != schema:
            print(f"drifted: {dest}")
            status = 1
    return status


def prune(fetched: dict[str, dict[str, object]]) -> list[Path]:
    """Vendored extension files no longer in the closure.

    Repinning an extension to a new version leaves the old version's schema on
    disk. Nothing reads it, but it would be vendored evidence of a version
    Portolan no longer approves, so the write path removes it.
    """
    keep = {_dest(url) for url in fetched}
    if not _EXTENSION_VENDOR_DIR.exists():
        return []
    return sorted(p for p in _EXTENSION_VENDOR_DIR.rglob("*.json") if p not in keep)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the vendored closure against upstream; exit 1 on drift",
    )
    args = parser.parse_args()

    fetched = crawl()
    stale = prune(fetched)
    if args.check:
        status = check(fetched)
        for path in stale:
            print(f"stale: {path}")
            status = 1
        return status
    write(fetched)
    for path in stale:
        path.unlink()
        print(f"removed stale: {path}")
    print(f"Vendored {len(fetched)} schema files -> {_SCHEMAS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
