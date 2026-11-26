"""Pure href interpretation and the catalog, crawl, structural, and live policies."""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePath, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, quote, unquote, urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

    from rashid_core.catalog import Node


def split_href(href: str) -> SplitResult | None:
    """Parse an untrusted href while keeping semicolons in its path."""
    try:
        return urlsplit(href)
    except ValueError:
        return None


def is_absolute_href(href: str) -> bool:
    """True for hrefs with a URI scheme or a leading slash."""
    parsed = split_href(href)
    return href.startswith("/") or parsed is None or bool(parsed.scheme)


def relative_href(source: str, target: str) -> str:
    """Relative URI reference between two already-encoded document paths."""
    path = posixpath.relpath(target, posixpath.dirname(source) or ".")
    return path if path.startswith("../") else "./" + path


def href_host(href: str) -> str:
    """Live authority key, using the shared authority normalization."""
    parsed = split_href(href)
    return url_authority(parsed) if parsed is not None else ""


def off_https_reference(href: str) -> bool:
    """Reject non-HTTPS redirects while allowing relative Location values."""
    parsed = split_href(href)
    return parsed is None or parsed.scheme.lower() not in ("", "https")


def self_link_base(root: Node | None) -> str | None:
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
        if isinstance(href, str) and split_href(href) is not None and is_absolute_href(href):
            prefix, sep, tail = containment_href(href).rpartition("/")
            if tail == root.path.name:
                return prefix + sep
    return None


def is_catalog_url(location: str) -> bool:
    """True when ``location`` is an https URL rather than a filesystem path."""
    parsed = split_href(location)
    return parsed is not None and is_https_href(location) and bool(parsed.netloc)


def split_catalog_url(url: str) -> tuple[str, str]:
    """``(base, root_url)`` for a catalog URL given either way.

    The URL may name the root ``catalog.json`` or the directory it sits in,
    with or without a trailing slash; both name the same catalog, as the
    directory and its ``catalog.json`` do on disk. The base always ends in one
    slash and the root URL is the base plus ``catalog.json``.
    """
    parsed = split_href(url)
    if parsed is None or not is_https_href(url) or not parsed.netloc:
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
    parsed = split_href(href)
    return parsed is not None and parsed.scheme.lower() == "https"


def containment_href(href: str) -> str:
    """Href spelling used for containment, without query or fragment."""
    return href.split("#", 1)[0].split("?", 1)[0]


def href_suffix(href: str) -> str:
    """The original query and fragment, including empty delimiters and escapes."""
    return href[len(containment_href(href)) :]


def store_member_href(href: str, member: str) -> str:
    """The href of ``member`` inside the store that ``href`` names.

    A Zarr store is a key prefix rather than one object, so its documents are
    addressed by appending path segments. Publishers write the store href
    both with and without a trailing slash, and ``member`` is a Zarr node
    path such as ``"0/rgba"``, so both sides are trimmed before they join.
    The result keeps the href's class: a relative store href yields a
    relative member href, which the asset reader resolves against the tree.

    The query and fragment move to the end of the result rather than being
    dropped. A store served from a signed URL carries its signature there, and
    a member href without it is refused by the host.
    """
    base = containment_href(href).rstrip("/")
    trimmed = member.strip("/")
    joined = f"{base}/{trimmed}" if trimmed else base
    return joined + href_suffix(href)


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
    joined = _relative_join(unquote(directory), path)
    return None if joined is None or has_host_drive(joined) else joined


# PureWindowsPath on Windows and PurePosixPath elsewhere.
_HOST_PATH: type[PurePath] = type(PurePath())


def has_host_drive(path: str) -> bool:
    """True when the host reads a path as drive-qualified, such as ``C:/x`` or ``C:x``.

    On Windows a drive makes a relative path leave the root when it joins the
    root path. On POSIX a colon is an ordinary character, so ``a:b`` is a
    directory name and has no drive.
    """
    return bool(host_path(path).drive)


def host_path(path: str) -> PurePath:
    """The path as the host compares it, case-insensitively on Windows."""
    return _HOST_PATH(path)


def resolve_local_href(document: str | PurePosixPath, href: str) -> PurePosixPath | None:
    """Resolve an href against the document that holds it, as the graph does on disk.

    ``document`` is the root-relative POSIX path of the linking file, with
    decoded filenames. The result is the target's root-relative path with
    percent escapes decoded and the query and fragment removed. The result is
    None for an empty or absolute href, for an href that leaves the tree, for
    one that decodes to a backslash or NUL, and on Windows for a result with a
    drive, such as ``C%3A/x``.
    """
    resolved = local_path(quote(str(PurePosixPath(document).parent), safe="/"), href)
    return PurePosixPath(resolved) if resolved is not None else None


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
    parsed = split_href(href)
    if parsed is None:
        return None
    if parsed.scheme or href.startswith("/"):
        if not is_https_href(href) or not href.startswith(base):
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
    parsed = split_href(base_url)
    if parsed is None or not is_https_href(base_url) or not parsed.netloc:
        raise ValueError(f"live base_url must be an https URL, got: {base_url!r}")
    return base_url.rstrip("/") + "/"


def live_host_href(directory: str, href: str, base: str | None) -> str | None:
    """Live policy: HTTPS on the publish authority, at any path on that host.

    wire_url encodes the resulting candidate's path for probing.
    """
    parsed = split_href(href)
    publish = split_href(base) if base is not None else None
    if parsed is None or (base is not None and publish is None):
        return None
    if is_https_href(href) and parsed.netloc:
        if publish is not None and url_authority(parsed) != url_authority(publish):
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
    parsed = split_href(url)
    if parsed is None:
        return None
    scheme = parsed.scheme.lower()
    try:
        _ = parsed.port
    except ValueError:
        return None
    netloc = url_authority(parsed)
    path = posixpath.normpath(parsed.path or "/")
    return scheme, netloc, path.rstrip("/") + "/"


# RFC 3986 sections 2 and 3: unreserved, sub-delims, and pct-encoded.
_URI_ATOM = r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[A-Fa-f0-9]{2})"
# RFC 3987 section 2.2: ucschar extends unreserved everywhere, and iprivate is
# allowed only in the query. Section 4.1 forbids the bidi formatting
# characters U+200E, U+200F, and U+202A to U+202E, so the first range skips them.
_UCSCHAR = (
    r"\u00a0-\u200d\u2010-\u2029\u202f-\ud7ff\uf900-\ufdcf\ufdf0-\uffef"
    + "".join(rf"\U{plane:04x}0000-\U{plane:04x}fffd" for plane in range(1, 14))
    + r"\U000e1000-\U000efffd"
)
_IPRIVATE = r"\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd"


def _reference_grammar(ucschar: str = "", iprivate: str = "") -> Callable[[str], bool]:
    """A syntax check for RFC 3986 references, or RFC 3987 ones given its ranges."""
    atom = rf"(?:[A-Za-z0-9._~!$&'()*+,;={ucschar}-]|%[A-Fa-f0-9]{{2}})"
    path = re.compile(rf"(?:{atom}|[:@/])*")
    query = re.compile(rf"(?:{atom}|[:@/?{iprivate}])*")
    fragment = re.compile(rf"(?:{atom}|[:@/?])*")
    # An IP literal stays ASCII in an IRI too.
    authority = re.compile(rf"(?:(?:{atom}|:)*@)?(?:{atom}*|\[(?:{_URI_ATOM}|:)+\])(?::[0-9]*)?")

    def check(href: str) -> bool:
        # urlsplit strips some whitespace and controls; reject them before parsing.
        if any(char <= " " or char == "\x7f" for char in href):
            return False
        parsed = split_href(href)
        if parsed is None:
            return False
        if not parsed.scheme and not href.startswith("/") and ":" in parsed.path.split("/", 1)[0]:
            return False
        return bool(
            path.fullmatch(parsed.path)
            and query.fullmatch(parsed.query)
            and fragment.fullmatch(parsed.fragment)
            and authority.fullmatch(parsed.netloc)
        )

    return check


_is_uri_reference = _reference_grammar()
_is_iri_reference = _reference_grammar(_UCSCHAR, _IPRIVATE)


def is_uri_reference(href: str) -> bool:
    """RFC 3986 URI-reference syntax, without resolving or decoding the href."""
    return _is_uri_reference(href)


def is_iri_reference(href: str) -> bool:
    """RFC 3987 IRI-reference syntax, without resolving or decoding the href.

    STAC 1.1 declares every link and asset ``href`` as an ``iri-reference``,
    so a raw ``./東京.parquet`` is valid there. A space, a control character,
    or a character outside ``ucschar`` (and ``iprivate`` in the query) is not.
    """
    return _is_iri_reference(href)


_NON_ASCII = re.compile(r"[^\x00-\x7f]+")
# RFC 3986 appendix B: the scheme and authority at the start of a reference.
_ORIGIN = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?(?://[^/?#]*)?")


def iri_to_uri(iri: str) -> str:
    """RFC 3987 section 3.1: percent-encode each non-ASCII character as UTF-8.

    Everything after the authority is mapped, so the path, query, and fragment
    of ``./東京.parquet?v=東`` go out as the URI a server stores them under.
    Existing escapes stay as they are. The authority is left to the transport,
    which converts an internationalized host name itself.
    """
    origin = _ORIGIN.match(iri)
    head = origin[0] if origin is not None else ""
    return head + _NON_ASCII.sub(lambda run: quote(run[0], safe=""), iri[len(head) :])


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

    A non-ASCII href is an IRI, and its path, query, and fragment are mapped
    to UTF-8 escapes (RFC 3987 section 3.1), as a server stores the object.

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
    encoded = quote(path, safe="/%:@!$&'()*+,;=")
    return f"{base}{origin}{encoded}{iri_to_uri(href_suffix(str(rel)))}"


# Schemes whose globs a client can list anonymously (formats.md, Partitioned
# Collections): Amazon S3 or an S3-compatible service, Google Cloud Storage,
# and Azure Blob Storage.
BUCKET_SCHEMES = ("s3", "gs", "az")


def bucket_glob(glob: str) -> tuple[str, str, str] | None:
    """``(scheme, bucket, key pattern)`` for a listable bucket glob, else None.

    The single owner of "a glob a client can list": an ``s3://``, ``gs://``,
    or ``az://`` URL with a bucket (or container) and a key pattern. The
    glob is split by hand, not as a URL: ``?`` is a wildcard and ``#`` a key
    character, so neither starts a query or a fragment.
    """
    match = _BUCKET_GLOB.fullmatch(glob.strip())
    if match is None or match["scheme"].lower() not in BUCKET_SCHEMES:
        return None
    return match["scheme"].lower(), match["bucket"], match["key"]


# A bucket or container name has no user, port, or path: letters, digits,
# dots, dashes, and underscores, as S3, GCS, and Azure allow.
_BUCKET_GLOB = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<bucket>[A-Za-z0-9][A-Za-z0-9._-]*)/(?P<key>.+)",
    re.DOTALL,
)
