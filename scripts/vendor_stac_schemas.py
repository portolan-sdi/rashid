"""Vendor the STAC core JSON Schema closure into the package, reproducibly.

The structural pass validates every object against the STAC core schemas for
its ``type``. Those schemas ship in the wheel (``src/rashid/_schemas/stac/``)
so the pass runs offline; this script is the only way they get there. It
crawls the transitive ``$ref`` closure from the three root schemas on
``schemas.stacspec.org`` and writes each file byte-identical to upstream —
the ``$id`` normalization that resolution needs happens at load time in
:mod:`rashid._jsonschema`, never on disk, so ``--check`` stays an honest diff
against upstream.

Usage::

    uv run python scripts/vendor_stac_schemas.py           # (re)write
    uv run python scripts/vendor_stac_schemas.py --check   # diff, exit 1 on drift

The spec-sync canary runs ``--check`` nightly: published STAC schemas are
immutable per version in principle, but a silent upstream edit would otherwise
change verdicts between rashid releases.

Ported from portolan-cli's ``scripts/refresh_stac_schemas.py``.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit

STAC_VERSION = "1.1.0"
_STAC_BASE = f"https://schemas.stacspec.org/v{STAC_VERSION}/"
# item.json reaches outside the STAC host: its GeoJSON geometry model is a
# $ref to geojson.org. Both hosts publish immutable, versioned-in-practice
# schemas, so both are vendorable; anything else in the closure is a bug.
_GEOJSON_BASE = "https://geojson.org/schema/"
_ROOTS = (
    f"{_STAC_BASE}catalog-spec/json-schema/catalog.json",
    f"{_STAC_BASE}collection-spec/json-schema/collection.json",
    f"{_STAC_BASE}item-spec/json-schema/item.json",
)

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "src" / "rashid" / "_schemas"
_STAC_VENDOR_DIR = _SCHEMAS_DIR / "stac" / STAC_VERSION
_GEOJSON_VENDOR_DIR = _SCHEMAS_DIR / "geojson"


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


def crawl() -> dict[str, dict[str, object]]:
    """Fetch the transitive $ref closure of the root schemas, keyed by URL."""
    fetched: dict[str, dict[str, object]] = {}
    queue: collections.deque[str] = collections.deque(_ROOTS)
    while queue:
        url = queue.popleft()
        if url in fetched:
            continue
        if not url.startswith((_STAC_BASE, _GEOJSON_BASE)):
            raise SystemExit(f"refusing ref outside the vendorable hosts: {url}")
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            schema: dict[str, object] = json.loads(response.read().decode("utf-8"))
        fetched[url] = schema
        for ref in _refs(schema):
            absolute = urljoin(url, ref.split("#", 1)[0])
            if absolute.startswith("https://") and absolute not in fetched:
                queue.append(absolute)
    return fetched


def _dest(url: str) -> Path:
    """Map a schema URL to its vendored path."""
    if url.startswith(_GEOJSON_BASE):
        return _GEOJSON_VENDOR_DIR / url.removeprefix(_GEOJSON_BASE)
    path = urlsplit(url).path
    return _STAC_VENDOR_DIR / path.split(f"/v{STAC_VERSION}/", 1)[1]


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the vendored closure against upstream; exit 1 on drift",
    )
    args = parser.parse_args()

    fetched = crawl()
    if args.check:
        return check(fetched)
    write(fetched)
    print(f"Vendored STAC v{STAC_VERSION} closure: {len(fetched)} files -> {_SCHEMAS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
