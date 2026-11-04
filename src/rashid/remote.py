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

import logging
import posixpath
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from rashid import _json, _store, _trace
from rashid._href import containment_href, crawl_mirror_path, crawl_prefix, href_suffix, split_href
from rashid.catalog import ROOT_CATALOG

log = logging.getLogger(__name__)

#: Documents fetched before the crawl stops enqueuing. Past this a catalog is
#: large enough that a full remote check wants a local sync anyway, and the
#: runner says the tree was cut short rather than pretending it was whole.
DEFAULT_MAX_DOCUMENTS = 10_000

#: Concurrent fetches per level of the tree. obstore releases the GIL during a
#: request and reuses one connection pool per host, so the threads overlap the
#: network waits rather than queue on them.
_WORKERS = 32

#: Largest document body the fetcher reads. A STAC document is kilobytes; a
#: link that lands on something far larger is not a document, and reading it
#: whole would let one href fill the disk the temporary tree sits on.
MAX_BODY_BYTES = 32 * 1024 * 1024

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


class _StoreFetcher:
    """The default fetcher: obstore, https only, redirects to https only."""

    def get(self, url: str) -> Fetched:
        try:
            body = _store.get(url, max_bytes=MAX_BODY_BYTES)
        except _store.StoreError as exc:
            if exc.status is not None:
                return Fetched(status=exc.status)
            raise FetchError(str(exc)) from exc
        return Fetched(status=200, body=body)


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
    trailing slash (see :func:`rashid._href.split_catalog_url`). Every document is written
    at ``dest / <path under base>``, the path spelled as the hrefs spell it, so
    the graph resolves the same hrefs to the same files. The URL requested,
    and recorded in ``statuses``, is that path percent-encoded, which is the
    URL the live pass and the data reader build for the same href. A document
    that answers non-2xx is not written, so the graph then lacks it exactly as
    the published tree does. The root's own failure to fetch raises
    :class:`FetchError` when no status came back; a status is recorded and
    left to the caller to judge.
    """
    if fetcher is None:
        fetcher = _StoreFetcher()
    result = Crawl(base=base)
    root = ROOT_CATALOG.name
    queue = _CrawlQueue(max_documents=max_documents)
    level = [root]
    depth = 0
    while level:
        log.debug("crawl depth %d: fetching %d document(s) under %s", depth, len(level), base)
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            outcomes = _trace.pool_map(
                pool, lambda rel: _fetch_one(fetcher, wire_url(base, rel)), level
            )
        depth += 1
        next_level: list[str] = []
        for rel, outcome in zip(level, outcomes, strict=True):
            url = wire_url(base, rel)
            if isinstance(outcome, str):
                result.errors[url] = outcome
                if rel == root:
                    raise FetchError(outcome)
                continue
            result.statuses[url] = outcome.status
            if not outcome.ok:
                continue
            path = containment_href(rel)
            written = _write(dest, path, outcome.body)
            if written is not None:
                result.errors[url] = written
                continue
            if not path.endswith(".json"):
                continue
            result.documents += 1
            _trace.count("crawl.documents")
            follows = _follow_paths(base, path, outcome.body)
            queue.enqueue(follows, next_level, result)
        level = next_level
    return result


@dataclass
class _CrawlQueue:
    """Track unique paths and the JSON document budget across crawl levels."""

    max_documents: int
    seen: set[str] = field(default_factory=lambda: {ROOT_CATALOG.name})
    queued_documents: int = 1  # root already queued

    def enqueue(self, follows: list[str], next_level: list[str], result: Crawl) -> None:
        for follow in follows:
            # The first URL for a document supplies its request suffix;
            # aliases must not consume the budget for distinct descendants.
            identity = containment_href(follow)
            if identity in self.seen:
                continue
            self.seen.add(identity)
            if containment_href(follow).endswith(".json"):
                if self.queued_documents >= self.max_documents:
                    result.capped = True
                    continue
                self.queued_documents += 1
            next_level.append(follow)


def wire_url(base: str, rel: str) -> str:
    """The URL for a path under the base: percent-encoded, existing escapes kept.

    The one place an href becomes a request URL, shared with the live pass
    and the data reader so the three agree on the URL for one href, and so a
    status the crawl recorded is found under the URL the live pass asks for.
    """
    path = containment_href(str(rel))
    parsed = split_href(path)
    origin = ""
    if parsed is not None and parsed.scheme and parsed.netloc:
        origin = path[: len(path) - len(parsed.path)] if parsed.path else path
        path = parsed.path
    return f"{base}{origin}{quote(path, safe='/%')}{href_suffix(str(rel))}"


def _fetch_one(fetcher: Fetcher, url: str) -> Fetched | str:
    try:
        return fetcher.get(url)
    except FetchError as exc:
        return str(exc)


def _write(dest: Path, rel: str, body: bytes) -> str | None:
    """Write ``body`` at ``dest / rel``; the reason when it could not be written.

    ``rel`` was normalized under the base by :func:`rashid._href.safe_crawl_path`, so the
    containment check should never fail; it exists because the alternative to
    a redundant check is a fetched document written outside the temporary
    tree. The filesystem can still refuse: a directory URL that answered 200
    leaves a file where a later document needs a directory, and an object
    store allows a name longer than a filesystem does. Either is reported,
    not raised.
    """
    path = crawl_mirror_path(rel)
    if path is None:
        return "unsafe document path or encoded '/' separator cannot be mirrored"
    root = dest.resolve()
    target = root.joinpath(*path.split("/"))
    try:
        target.relative_to(root)
    except ValueError:  # pragma: no cover - guarded upstream
        return "path is not below the temporary tree"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not target.is_file():
            return "a directory already occupies the path"
        target.write_bytes(body)
    except OSError as exc:
        return f"cannot write the document to the temporary tree: {exc.strerror or exc}"
    return None


def _follow_paths(base: str, rel: str, body: bytes) -> list[str]:
    """The paths a fetched document leads to: sidecars, then the links to descend.

    Only a STAC catalog, collection, or item is followed; any other JSON — a
    style file named by an ``item`` link by mistake, say — leads nowhere. A
    catalog or collection also brings its directory's ``AGENTS.md`` and
    ``README.md``, so the ``PTL-FIL`` rules see what a directory walk would.
    """
    try:
        data = _json.loads(body)
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
        path = crawl_prefix(base, directory, href)
        # Only a JSON document is followed and written. A child or item link
        # to anything else is not a STAC object; the live pass still HEADs it.
        if path is not None and containment_href(path).endswith(".json"):
            paths.append(path)
    return paths


__all__ = [
    "DEFAULT_MAX_DOCUMENTS",
    "MAX_BODY_BYTES",
    "Crawl",
    "FetchError",
    "Fetched",
    "Fetcher",
    "crawl",
    "wire_url",
]
