"""Pure href interpretation and the catalog, crawl, structural, and live policies."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import ParseResult, SplitResult, quote, unquote, urljoin, urlparse, urlsplit


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
    parsed = parse_href(href)
    return href.startswith("/") or parsed is None or bool(parsed.scheme)


def href_host(href: str) -> str:
    """Live authority key, retaining the declared port and ignoring host case."""
    parsed = split_href(href)
    return parsed.netloc.lower() if parsed is not None else ""


def off_https_reference(href: str) -> bool:
    """Reject non-HTTPS redirects while allowing relative Location values."""
    parsed = split_href(href)
    return parsed is None or parsed.scheme.lower() not in ("", "https")


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
        if isinstance(href, str) and parse_href(href) is not None and is_absolute_href(href):
            prefix, sep, tail = containment_href(href).rpartition("/")
            if tail == root.path.name:
                return prefix + sep
    return None


def is_catalog_url(location: str) -> bool:
    """True when ``location`` is an https URL rather than a filesystem path."""
    parsed = parse_href(location)
    return parsed is not None and parsed.scheme.lower() == "https" and bool(parsed.netloc)


def split_catalog_url(url: str) -> tuple[str, str]:
    """``(base, root_url)`` for a catalog URL given either way.

    The URL may name the root ``catalog.json`` or the directory it sits in,
    with or without a trailing slash; both name the same catalog, as the
    directory and its ``catalog.json`` do on disk. The base always ends in one
    slash and the root URL is the base plus ``catalog.json``.
    """
    parsed = parse_href(url)
    if parsed is None or parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"catalog URL must be https, got: {url!r}")
    parsed = parsed._replace(query="", fragment="")
    parsed = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower())
    path = parsed.path
    if path.endswith("/catalog.json") or path == "catalog.json":
        path = path[: -len("catalog.json")]
    path = path.rstrip("/") + "/"
    base = parsed._replace(path=path).geturl()
    return base, f"{base}catalog.json"


def is_https_href(href: str) -> bool:
    """Data-reader policy: an HTTPS scheme, even without an authority."""
    parsed = parse_href(href)
    return parsed is not None and parsed.scheme.lower() == "https"


def is_schema_url(href: str) -> bool:
    """Schema-fetch policy: the literal, case-sensitive HTTPS prefix."""
    return href.startswith("https://")


def containment_href(href: str) -> str:
    """Href spelling used for containment, without query or fragment."""
    return href.split("#", 1)[0].split("?", 1)[0]


def href_suffix(href: str) -> str:
    """The original query and fragment, including empty delimiters and escapes."""
    return href[len(containment_href(href)) :]


def _relative_join(directory: str, path: str) -> str | None:
    joined = posixpath.normpath(posixpath.join(directory, path))
    if joined == "." or joined.startswith(".."):
        return None
    return joined


def relative_path(directory: str, href: str) -> str | None:
    """Encoded, root-relative document identity; never a decoded filename."""
    if not href or is_absolute_href(href):
        return None
    return _relative_join(directory, containment_href(href))


def local_path(directory: str, href: str) -> str | None:
    """Decode only a classified relative href at the local filesystem boundary."""
    if relative_path(directory, href) is None:
        return None
    path = unquote(containment_href(href))
    if posixpath.isabs(path) or "\\" in path or "\x00" in path:
        return None
    return _relative_join(unquote(directory), path)


def request_target(directory: str, href: str) -> str | None:
    """Encoded request target, including its original query and fragment."""
    path = relative_path(directory, href)
    return path + href_suffix(href) if path is not None else None


def structural_prefix(href: str, base: str | None) -> str | None:
    """Structural policy: absolute hrefs must start with the language self base.

    Relative hrefs resolve from their document; absolute hrefs resolve from
    the language root after removing this prefix. No HTTPS restriction applies.
    """
    href = containment_href(href)
    if not is_absolute_href(href):
        return href
    if base is None or not href.startswith(base):
        return None
    return href[len(base) :]


def crawl_prefix(base: str, directory: str, href: str) -> str | None:
    """The root-relative href under ``base``, retaining its request suffix.

    A relative href joins onto the document's directory. An absolute https
    href is followed only when it starts with the base — a link to another
    host is another catalog's business, and the live pass's PORTO-CORE-073
    scoping says the same. Either way the result is normalized and must stay
    below the root: a catalog is a hostile input here, and a path that climbs
    out of the tree would otherwise name where its bytes get written.
    """
    parsed = parse_href(href)
    if parsed is None:
        return None
    if parsed.scheme or href.startswith("/"):
        if parsed.scheme.lower() != "https" or not href.startswith(base):
            return None
        candidate = containment_href(href[len(base) :])
    else:
        candidate = posixpath.join(directory, containment_href(href))
    path = safe_crawl_path(candidate)
    return path + href_suffix(href) if path is not None else None


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


def crawl_mirror_path(href: str) -> str | None:
    """Keep encoded mirror names; reject separators that hide STAC filenames."""
    path = safe_crawl_path(containment_href(href))
    return path if path is not None and not _ENCODED_SLASH.search(path) else None


def normalize_live_base(base_url: str) -> str:
    """Validate and normalize a publish base URL to https with one trailing slash.

    https-only for the same reason the data reader is: the base joins with
    catalog-controlled hrefs into request URLs, so any weaker scheme would let
    a hostile tree downgrade or redirect the probes.
    """
    parsed = parse_href(base_url)
    if parsed is None or parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"live base_url must be an https URL, got: {base_url!r}")
    return base_url.rstrip("/") + "/"


def live_host_href(directory: str, href: str, base: str | None) -> str | None:
    """Live policy: HTTPS on the publish authority, at any path on that host.

    wire_url encodes the resulting candidate's path for probing.
    """
    parsed = parse_href(href)
    publish = parse_href(base) if base is not None else None
    if parsed is None or (base is not None and publish is None):
        return None
    if parsed.scheme.lower() == "https" and parsed.netloc:
        if publish is not None and parsed.netloc.lower() != publish.netloc.lower():
            return None
        return href
    if base is None:
        return None
    target = request_target(directory, href)
    return base + target if target is not None else None


def resolve_live_base(
    explicit: str | None, crawled: str | None, claimed: str | None
) -> tuple[str | None, bool]:
    """Explicit base wins over crawl base, then a fetchable root self base.

    The boolean distinguishes caller-provided locations from self claims.
    The caller supplies its crawl base and root self claim.
    """
    named = explicit if explicit is not None else crawled
    if named is not None:
        return normalize_live_base(named), True
    if claimed is not None and is_catalog_url(claimed):
        return normalize_live_base(claimed), False
    return None, False


def location_key(url: str) -> tuple[str, str, str] | None:
    parsed = parse_href(url)
    if parsed is None:
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return None
    default = {"https": 443, "http": 80}.get(scheme)
    netloc = host if port is None or port == default else f"{host}:{port}"
    path = posixpath.normpath(parsed.path or "/")
    return scheme, netloc, path.rstrip("/") + "/"


# RFC 3986 sections 2 and 3: unreserved, sub-delims, and pct-encoded.
_URI_ATOM = r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[A-Fa-f0-9]{2})"
_URI_PATH = re.compile(rf"(?:{_URI_ATOM}|[:@/])*")
_URI_QUERY = re.compile(rf"(?:{_URI_ATOM}|[:@/?])*")
_URI_AUTHORITY = re.compile(
    rf"(?:(?:{_URI_ATOM}|:)*@)?"
    rf"(?:{_URI_ATOM}*|\[(?:{_URI_ATOM}|:)+\])(?::[0-9]*)?"
)


def is_uri_reference(href: str) -> bool:
    """RFC 3986 URI-reference syntax, without resolving or decoding the href."""
    # urlsplit strips some whitespace and controls; reject them before parsing.
    if not href.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in href):
        return False
    parsed = split_href(href)
    if parsed is None:
        return False
    if not parsed.scheme and not href.startswith("/") and ":" in parsed.path.split("/", 1)[0]:
        return False
    return bool(
        _URI_PATH.fullmatch(parsed.path)
        and _URI_QUERY.fullmatch(parsed.query)
        and _URI_QUERY.fullmatch(parsed.fragment)
        and _URI_AUTHORITY.fullmatch(parsed.netloc)
    )


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


def object_location(url: str, *, base: str | None = None) -> tuple[str, str, str, str] | None:
    """Identify objects by the path transport requests.

    obstore uses the decoded store path; urllib preserves the URL path.
    Only file URLs use filesystem path normalization.
    """
    try:
        resolved = urljoin(base, url) if base is not None else url
    except ValueError:
        return None
    parsed = split_href(resolved)
    if parsed is None:
        return None
    if parsed.scheme == "file":
        path = posixpath.normpath(unquote(parsed.path))
    else:
        stored = store_path(parsed)
        path = parsed.path if stored is None else stored
    return parsed.scheme.lower(), url_authority(parsed), path, parsed.query


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
