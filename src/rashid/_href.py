"""Pure href interpretation and the catalog, crawl, structural, and live policies."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import ParseResult, SplitResult, unquote, urlparse, urlsplit


class _SelfLinked(Protocol):
    path: PurePosixPath
    data: dict[str, Any]


def parse_href(href: str) -> ParseResult | None:
    """Parse an untrusted href; malformed authorities have no parsed value."""
    try:
        return urlparse(href)
    except ValueError:
        return None


def split_href(href: str) -> SplitResult | None:
    """Parse an untrusted href while keeping semicolons in its path."""
    try:
        return urlsplit(href)
    except ValueError:
        return None


def is_absolute_href(href: str) -> bool:
    """True for hrefs with a URI scheme or a leading slash."""
    return href.startswith("/") or bool(urlparse(href).scheme)


def self_link_base(root: _SelfLinked | None) -> str | None:
    """The URL prefix a root catalog is published under, from its ``self`` link.

    core.md, Links: a catalog served over the internet from a single fixed
    URL SHOULD carry an absolute ``self`` link on its root catalog
    (PORTO-CORE-081). That link names the published location of the root
    file, so the URL minus the root file name is the base every file in the
    tree is served under. None when ``root`` is None, carries no absolute
    ``self`` link, or its ``self`` link does not end in the root's own file
    name — a ``self`` naming some other file says nothing about this tree.
    """
    if root is None:
        return None
    raw = root.data.get("links")
    for link in raw if isinstance(raw, list) else []:
        if not isinstance(link, dict) or link.get("rel") != "self":
            continue
        href = link.get("href")
        if isinstance(href, str) and is_absolute_href(href):
            prefix, sep, tail = href.rpartition("/")
            if tail == root.path.name:
                return prefix + sep
    return None


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
    parsed = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower())
    path = parsed.path
    if path.endswith("/catalog.json") or path == "catalog.json":
        path = path[: -len("catalog.json")]
    path = path.rstrip("/") + "/"
    base = parsed._replace(path=path).geturl()
    return base, f"{base}catalog.json"


def is_https_href(href: str) -> bool:
    """Data-reader policy: an HTTPS scheme, even without an authority."""
    return urlparse(href).scheme.lower() == "https"


def is_schema_url(href: str) -> bool:
    """Schema-fetch policy: the literal, case-sensitive HTTPS prefix."""
    return href.startswith("https://")


def relative_path(directory: str, href: str) -> str | None:
    """Graph policy: a relative href normalized below the catalog root."""
    if not href or is_absolute_href(href):
        return None
    joined = posixpath.normpath(posixpath.join(directory, href))
    if joined == "." or joined.startswith(".."):
        return None
    return joined


def structural_prefix(href: str, base: str | None) -> str | None:
    """Structural policy: absolute hrefs must start with the language self base.

    Relative hrefs resolve from their document; absolute hrefs resolve from
    the language root after removing this prefix. No HTTPS restriction applies.
    """
    if not is_absolute_href(href):
        return href
    if base is None or not href.startswith(base):
        return None
    return href[len(base) :]


def crawl_prefix(base: str, directory: str, href: str) -> str | None:
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
    return safe_crawl_path(candidate)


def safe_crawl_path(candidate: str) -> str | None:
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


def normalize_live_base(base_url: str) -> str:
    """Validate and normalize a publish base URL to https with one trailing slash.

    https-only for the same reason the data reader is: the base joins with
    catalog-controlled hrefs into request URLs, so any weaker scheme would let
    a hostile tree downgrade or redirect the probes.
    """
    parsed = urlparse(base_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"live base_url must be an https URL, got: {base_url!r}")
    return base_url.rstrip("/") + "/"


def live_host_href(directory: str, href: str, base: str | None) -> str | None:
    """Live policy: HTTPS on the publish authority, at any path on that host.

    The result is unencoded; remote.wire_url remains the wire encoder.
    T4 adapts graph nodes to this pure policy.
    """
    parsed = urlparse(href)
    if parsed.scheme.lower() == "https" and parsed.netloc:
        if base is not None and parsed.netloc.lower() != urlparse(base).netloc.lower():
            return None
        return href
    if base is None:
        return None
    rel = relative_path(directory, href)
    return base + rel if rel is not None else None


def resolve_live_base(
    explicit: str | None, crawled: str | None, claimed: str | None
) -> tuple[str | None, bool]:
    """Explicit base wins over crawl base, then a fetchable root self base.

    The boolean distinguishes caller-provided locations from self claims.
    T4 supplies the graph's two bases at its existing call sites.
    """
    named = explicit if explicit is not None else crawled
    if named is not None:
        return normalize_live_base(named), True
    if claimed is not None and is_catalog_url(claimed):
        return normalize_live_base(claimed), False
    return None, False


def location_key(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    port = parsed.port
    default = {"https": 443, "http": 80}.get(scheme)
    netloc = host if port is None or port == default else f"{host}:{port}"
    path = posixpath.normpath(parsed.path or "/")
    return scheme, netloc, path.rstrip("/") + "/"


_ENCODED_SLASH = re.compile(r"%2f", re.IGNORECASE)


def url_authority(parsed: SplitResult) -> str:
    """Case-insensitive authority, omitting an explicit default HTTP port."""
    authority = parsed.netloc.lower()
    default = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    try:
        if default is not None and parsed.port == default:
            authority = authority.rsplit(":", 1)[0]
    except ValueError:
        pass  # Malformed ports are reported elsewhere.
    return authority


def store_path(parsed: SplitResult) -> str | None:
    """The decoded object path, or None when transport must preserve the URL."""
    if parsed.query or _ENCODED_SLASH.search(parsed.path):
        return None
    path = unquote(parsed.path).lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def object_location(url: str) -> tuple[str, str, str, str]:
    """Identify objects by the path transport requests.

    obstore uses the decoded store path; urllib preserves the URL path.
    Only file URLs use filesystem path normalization.
    """
    parsed = urlsplit(url)
    if parsed.scheme == "file":
        path = posixpath.normpath(unquote(parsed.path))
    else:
        stored = store_path(parsed)
        path = parsed.path if stored is None else stored
    return parsed.scheme.lower(), url_authority(parsed), path, parsed.query
