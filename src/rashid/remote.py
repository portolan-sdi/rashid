"""Read a published catalog over https into a local tree.

``rashid check`` reads a directory. A published catalog is a set of documents
behind one base URL, and the only view a validator has of it is the one a
client has: the root ``catalog.json``, and whatever its links reach. This
module follows those links — ``child``, ``item``, and the ``alternate`` links
that join alternate-language trees — and writes each document under a local
directory at the path its URL has under the base. The result is a tree
:class:`~rashid.catalog.CatalogGraph` loads like any other, so every pass runs
over it unchanged.

Two things separate the tree from a directory walk. The crawl sees only what a
link names, so an object no link reaches — the orphan ``PTL-LNK-002`` exists
to catch — is invisible, and so are the scene files ``PTL-COL-005`` looks for
beside a collection; the runner reports that limit once. And a catalog or
collection directory's ``AGENTS.md`` and ``README.md`` are fetched by
convention rather than through their links, because the ``PTL-FIL`` rules ask
whether the files exist beside the object and then whether the links name
them; fetching through the links would make a wrong link hide a present file.

The crawl records the status of every URL it asked. The live pass takes that
record as ``known_statuses`` so it does not HEAD the same documents again
(``PTL-LIV-006`` reads a 404 off the crawl instead), and the runner reads the
root's status to report a catalog that is not there. Fetches are https-only,
like every other read this package makes, and each level of the tree is
fetched concurrently since the documents are small and many.
"""

from __future__ import annotations

import json
import posixpath
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from rashid._http import user_agent
from rashid.catalog import ROOT_CATALOG

#: Documents fetched before the crawl stops enqueuing. Past this a catalog is
#: large enough that a full remote check wants a local sync anyway, and the
#: runner says the tree was cut short rather than pretending it was whole.
DEFAULT_MAX_DOCUMENTS = 10_000

#: Concurrent fetches per level of the tree.
_WORKERS = 8

_TIMEOUT = 30  # seconds per request

# Files fetched beside every catalog and collection, whether linked or not.
_SIDECARS = ("AGENTS.md", "README.md")

# Links the crawl descends. ``alternate`` only when it declares JSON: the same
# rel also names HTML renderings of the object.
_DESCEND_RELS = frozenset({"child", "item"})


class FetchError(Exception):
    """A request that produced no HTTP status at all: DNS, TLS, timeout."""


@dataclass(frozen=True)
class Fetched:
    """What one GET returned: the status and, for a 2xx, the body."""

    status: int
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Fetcher(Protocol):
    """GET one https URL. Raises :class:`FetchError` when no status came back."""

    def get(self, url: str) -> Fetched: ...


class _UrllibFetcher:
    def get(self, url: str) -> Fetched:
        if urlparse(url).scheme.lower() != "https":
            raise ValueError(f"refusing to fetch non-https URL: {url!r}")
        request = Request(url, method="GET", headers={"User-Agent": user_agent()})
        try:
            with urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310  # nosec B310
                return Fetched(status=response.status, body=response.read())
        except HTTPError as exc:
            return Fetched(status=exc.code)
        except Exception as exc:  # noqa: BLE001 - urllib raises a zoo; the message is what matters
            raise FetchError(f"GET {url}: {exc}") from exc


def is_catalog_url(location: str) -> bool:
    """True when ``location`` is an https URL rather than a filesystem path."""
    parsed = urlparse(location)
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def split_catalog_url(url: str) -> tuple[str, str]:
    """``(base, root_url)`` for a catalog URL given either way.

    The URL may name the root ``catalog.json`` or the directory it sits in,
    with or without a trailing slash; both name the same catalog, as the
    directory and its ``catalog.json`` do on disk. The base always ends in one
    slash and the root URL is the base plus ``catalog.json``.
    """
    if not is_catalog_url(url):
        raise ValueError(f"catalog URL must be https, got: {url!r}")
    parsed = urlparse(url)._replace(query="", fragment="")
    path = parsed.path
    if path.endswith(f"/{ROOT_CATALOG.name}") or path == ROOT_CATALOG.name:
        path = path[: -len(ROOT_CATALOG.name)]
    path = path.rstrip("/") + "/"
    base = parsed._replace(path=path).geturl()
    return base, f"{base}{ROOT_CATALOG.name}"


@dataclass
class Crawl:
    """What a crawl found: every URL asked, and how the tree was assembled.

    Attributes:
        base: The publish base every document was fetched under.
        statuses: URL to the HTTP status it answered, for every URL asked,
            sidecars included. A URL whose fetch raised is absent here and
            present in ``errors``.
        errors: URL to the transport failure that produced no status.
        documents: STAC JSON documents the crawl wrote.
        capped: True when the document limit stopped the crawl before the
            tree ran out of links.
    """

    base: str
    statuses: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    documents: int = 0
    capped: bool = False


def crawl(
    base: str,
    dest: Path,
    fetcher: Fetcher | None = None,
    *,
    max_documents: int = DEFAULT_MAX_DOCUMENTS,
) -> Crawl:
    """Fetch the tree under ``base`` into ``dest``, following links from the root.

    ``base`` is the directory URL the root ``catalog.json`` sits under, with a
    trailing slash (see :func:`split_catalog_url`). Every document is written
    at ``dest / <path under base>``, the path spelled as the hrefs spell it, so
    the graph resolves the same hrefs to the same files; only the request URL
    is percent-encoded. A document that answers non-2xx is not written, so the
    graph then lacks it exactly as the published tree does. The root's own
    failure to fetch raises :class:`FetchError` when no status came back; a
    status is recorded and left to the caller to judge.
    """
    if fetcher is None:
        fetcher = _UrllibFetcher()
    result = Crawl(base=base)
    root = ROOT_CATALOG.name
    seen = {root}
    level = [root]
    while level:
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            outcomes = list(pool.map(lambda rel: _fetch_one(fetcher, _wire_url(base, rel)), level))
        next_level: list[str] = []
        for rel, outcome in zip(level, outcomes, strict=True):
            url = f"{base}{rel}"
            if isinstance(outcome, str):
                result.errors[url] = outcome
                if rel == root:
                    raise FetchError(outcome)
                continue
            result.statuses[url] = outcome.status
            if not outcome.ok or not _write(dest, rel, outcome.body):
                continue
            if not rel.endswith(".json"):
                continue
            result.documents += 1
            for follow in _follow_paths(base, rel, outcome.body):
                if follow in seen:
                    continue
                seen.add(follow)
                if follow.endswith(".json") and (
                    result.documents + _pending_documents(next_level) >= max_documents
                ):
                    result.capped = True
                    continue
                next_level.append(follow)
        level = next_level
    return result


def _wire_url(base: str, rel: str) -> str:
    """The URL to request: the raw path percent-encoded, existing escapes kept."""
    return f"{base}{quote(rel, safe='/%')}"


def _pending_documents(rels: list[str]) -> int:
    return sum(1 for rel in rels if rel.endswith(".json"))


def _fetch_one(fetcher: Fetcher, url: str) -> Fetched | str:
    try:
        return fetcher.get(url)
    except FetchError as exc:
        return str(exc)


def _write(dest: Path, rel: str, body: bytes) -> bool:
    """Write ``body`` at ``dest / rel``; False when the path is not below ``dest``.

    ``rel`` was normalized under the base by :func:`_safe_relative`, so this
    second check should never fail; it exists because the alternative to a
    redundant check is a fetched document written outside the temporary tree.
    """
    root = dest.resolve()
    target = root.joinpath(*rel.split("/"))
    try:
        target.relative_to(root)
    except ValueError:  # pragma: no cover - guarded upstream
        return False
    if target.exists() and not target.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return True


def _follow_paths(base: str, rel: str, body: bytes) -> list[str]:
    """The paths a fetched document leads to: sidecars, then the links to descend.

    Only a STAC catalog, collection, or item is followed; any other JSON — a
    style file named by an ``item`` link by mistake, say — leads nowhere. A
    catalog or collection also brings its directory's ``AGENTS.md`` and
    ``README.md``, so the ``PTL-FIL`` rules see what a directory walk would.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    stac_type = data.get("type")
    if stac_type not in ("Catalog", "Collection", "Feature"):
        return []
    directory = posixpath.dirname(rel)
    paths: list[str] = []
    if stac_type in ("Catalog", "Collection"):
        paths.extend(posixpath.join(directory, name) for name in _SIDECARS)
    raw = data.get("links")
    for link in raw if isinstance(raw, list) else []:
        if not isinstance(link, dict):
            continue
        rel_type = link.get("rel")
        if rel_type not in _DESCEND_RELS and not (
            rel_type == "alternate" and link.get("type") == "application/json"
        ):
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href:
            continue
        path = _under_base(base, directory, href)
        if path is not None:
            paths.append(path)
    return paths


def _under_base(base: str, directory: str, href: str) -> str | None:
    """The path under ``base`` that ``href`` names; None when it is not the tree's.

    A relative href joins onto the document's directory. An absolute https
    href is followed only when it starts with the base — a link to another
    host is another catalog's business, and the live pass's PORTO-CORE-073
    scoping says the same. Either way the result is normalized and must stay
    below the root: a catalog is a hostile input here, and a path that climbs
    out of the tree would otherwise name where its bytes get written.
    """
    parsed = urlparse(href)
    if parsed.scheme or href.startswith("/"):
        if parsed.scheme.lower() != "https" or not href.startswith(base):
            return None
        candidate = href[len(base) :].split("#", 1)[0].split("?", 1)[0]
    else:
        candidate = posixpath.join(directory, href.split("#", 1)[0].split("?", 1)[0])
    return _safe_relative(candidate)


def _safe_relative(candidate: str) -> str | None:
    """``candidate`` normalized as a path strictly below the root, or None.

    Rejects the root itself, anything that climbs above it, an absolute path,
    a backslash (a separator on the filesystem the tree is written to, and
    never legitimate in an href), and any segment that is empty or dots-only
    after normalization.
    """
    if not candidate or "\\" in candidate or "\x00" in candidate:
        return None
    rel = posixpath.normpath(candidate)
    if rel in (".", "..") or rel.startswith(("../", "/")):
        return None
    if any(part in ("", ".", "..") for part in rel.split("/")):
        return None  # pragma: no cover - normpath leaves none; belt and braces
    return rel


__all__ = [
    "DEFAULT_MAX_DOCUMENTS",
    "Crawl",
    "FetchError",
    "Fetched",
    "Fetcher",
    "crawl",
    "is_catalog_url",
    "split_catalog_url",
]
