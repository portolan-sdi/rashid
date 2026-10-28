"""Live-hosting pass: probe the servers behind remote assets for the Data
Storage MUSTs — HTTP range support and CORS (core.md, Data Storage).

The metadata and data passes read what is *in* the catalog; whether the hosting
server honors ``Range`` or lets a browser read across origins is a property of
the server itself, checkable only by probing it. Three probes per host cover
the section's MUSTs:

- a **ranged GET** (with an ``Origin`` header) — ``206 Partial Content``,
  ``Accept-Ranges: bytes``, and the simple-response CORS headers. Servers omit
  every ``Access-Control-*`` header unless the request carries ``Origin``, so
  sending one is required, not optional.
- a **HEAD** per asset — an accurate ``Content-Length`` (checked for presence,
  and against the declared ``file:size`` when the asset carries one).
- an **OPTIONS preflight** (``Access-Control-Request-Method: GET``, and
  ``Access-Control-Request-Headers`` naming ``Range`` plus the
  conditional-request headers) — allowed methods and request headers appear
  only on preflight responses, never on GET/HEAD.

The section scopes its MUSTs to the servers hosting the catalog's own
cloud-native assets and exempts bytes a third party hosts, so the target set is
built from who serves an asset rather than from the roles it carries. Given
``base_url``, an asset is the catalog's own when its href is relative (it lives
in the published tree) or absolute on the publish host; an absolute href on any
other host is upstream and is skipped. A census.gov ZIP that ignores ``Range``
says nothing about the catalog citing it. Without ``base_url`` there is no
publish host to compare against, so every absolute href is probed as declared
and the PORTO-CORE-073 carve-out cannot be applied — pass ``base_url`` for a
catalog that cites upstream copies.

Range and CORS semantics are server properties, so the GET and OPTIONS probes
run once per distinct host (via its lexically first asset); only the cheap HEAD
runs per asset. Absolute ``https`` hrefs are probed as declared; relative
hrefs are probed when the caller supplies ``base_url`` — the URL the catalog
root is published under — by joining the root-relative asset path onto it.
``s3`` and friends are ``PTL-AST-002``'s domain. The pass needs no geospatial
stack and lives in core; like every optional pass it degrades to
``PTL-LIV-000`` warnings when it cannot probe rather than failing the run.

Given ``base_url`` the pass also asks whether the documents behind the
catalog's **links** exist on the publish host (``PTL-LIV-006``). core.md,
Links: every link MUST resolve (PORTO-CORE-035), against the catalog's own
tree "whether on a local filesystem or on object storage" (PORTO-CORE-036).
``PTL-LNK-006`` settles that for the local tree; a publisher that uploads the
assets but not the nested ``catalog.json`` and item documents leaves a tree
whose ``child`` and ``item`` links answer 404 while every asset probe passes,
and only the publish host can say so. Every link target that resolves under
the base — ``child``, ``item``, ``parent``, ``root``, ``self``, ``agents``,
``describedby``, ``alternate``, a relative ``license``, and any other rel — is
HEADed once per distinct URL, in URL order; the same PORTO-CORE-073 host
scoping as for assets keeps the probes off other hosts. There is no GET
fallback on a 405: PORTO-CORE-043 makes HEAD a MUST for the publish host, so a
host that rejects HEAD is reported, not worked around.

No probe leaves https. A redirect to another https URL is followed, and the
checks read the answer at its end. A redirect to any other scheme is not
followed: a client that stays on https gets no object there, so the asset or
link is reported as ``PTL-LIV-008``, with the URL the redirect names. When
the host's first asset redirects that way, the range and CORS probes move to
the first asset whose HEAD stays on https; when none does, ``PTL-LIV-000``
says they did not run.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse

from rashid import _store, _trace
from rashid._http import user_agent
from rashid.catalog import CatalogGraph, Kind, Node, is_absolute_href
from rashid.model import Finding, Severity
from rashid.remote import wire_url
from rashid.rules._common import links_of

if TYPE_CHECKING:
    from urllib.request import Request

log = logging.getLogger(__name__)

LIV_UNAVAILABLE = "PTL-LIV-000"
LIV_RANGE = "PTL-LIV-001"
LIV_HEAD_LENGTH = "PTL-LIV-002"
LIV_CORS_ORIGIN = "PTL-LIV-003"
LIV_CORS_EXPOSE = "PTL-LIV-004"
LIV_CORS_PREFLIGHT = "PTL-LIV-005"
LIV_LINK_TARGET = "PTL-LIV-006"
LIV_SELF_BASE = "PTL-LIV-007"
LIV_OFF_HTTPS = "PTL-LIV-008"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
SPEC_IDS: dict[str, tuple[str, ...]] = {
    # Every live check probes the host set built below, and that set is where
    # PORTO-CORE-073 lands: an absolute href on a host other than the one the
    # catalog is published under is dropped before a host is ever collected,
    # so no upstream server is asked to satisfy the Data Storage MUSTs.
    LIV_RANGE: ("PORTO-CORE-043", "PORTO-CORE-073"),
    LIV_HEAD_LENGTH: ("PORTO-CORE-043", "PORTO-CORE-073"),
    LIV_CORS_ORIGIN: ("PORTO-CORE-045", "PORTO-CORE-073"),
    LIV_CORS_EXPOSE: ("PORTO-CORE-045", "PORTO-CORE-073"),
    LIV_CORS_PREFLIGHT: ("PORTO-CORE-045", "PORTO-CORE-073"),
    # core.md, Links: every link MUST resolve (035), against the catalog's own
    # tree on object storage as much as on disk (036). The publish host is
    # that tree; the same PORTO-CORE-073 scoping keeps the HEADs off others.
    LIV_LINK_TARGET: ("PORTO-CORE-035", "PORTO-CORE-036", "PORTO-CORE-073"),
    # core.md, Links: a published root catalog SHOULD carry an absolute self
    # link (081). When the caller names the publish base too, the two must
    # agree, or the publish step wrote the catalog to the wrong location.
    LIV_SELF_BASE: ("PORTO-CORE-081",),
    # core.md, Assets: an absolute href MUST use https, because a browser
    # cannot fetch the other schemes (023). A redirect off https moves the
    # object to such a URL, so a link that redirects there does not resolve
    # for an https client either (035). The 073 scoping applies as above.
    LIV_OFF_HTTPS: ("PORTO-CORE-023", "PORTO-CORE-035", "PORTO-CORE-073"),
}

# Assets are declared on collections and items; catalogs carry none.
_LIVE_KINDS: tuple[Kind, ...] = ("collection", "item")

# Links are declared on every object kind.
_LINK_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")

_TIMEOUT = 30  # seconds per request

# Concurrent HEADs against one host.
_WORKERS = 32

# An arbitrary origin: a read-permitting CORS policy answers any origin, and a
# restrictive one will not match this, correctly failing the check.
_PROBE_ORIGIN = "https://rashid-live-probe.invalid"

# core.md, Data Storage — the response headers a server MUST expose to browsers.
_REQUIRED_EXPOSED = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
)

# core.md, Data Storage — the request headers a server MUST allow. The
# conditional ones let a browser revalidate a cached range instead of
# refetching it.
_REQUIRED_REQUEST = (
    "Range",
    "If-Match",
    "If-Modified-Since",
    "If-None-Match",
    "If-Unmodified-Since",
)


@dataclass(frozen=True)
class ProbeResponse:
    """One HTTP response, reduced to what the checks read.

    ``headers`` maps lowercased header names to raw values.
    """

    status: int
    headers: dict[str, str]

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def off_https(self) -> str | None:
        """The non-https URL this response redirects to; None for any other answer.

        A redirect that stays on https is followed before a response is
        built, so a 3xx seen here points off https or names no location. A
        relative ``Location`` keeps the request's https scheme.
        """
        if not 300 <= self.status < 400:
            return None
        location = self.header("location")
        if location is None or urlparse(location).scheme.lower() in ("", "https"):
            return None
        return location


class Prober(Protocol):
    """Issues the three probe requests against one URL.

    :meth:`head` runs on up to ``_WORKERS`` threads at once, one URL each, so
    an implementation must be safe to call concurrently. :meth:`get_range`
    and :meth:`preflight` run on the calling thread only.
    """

    def get_range(self, url: str) -> ProbeResponse:
        """GET with ``Range: bytes=0-0`` and an ``Origin`` header."""

    def head(self, url: str) -> ProbeResponse:
        """Plain HEAD."""

    def preflight(self, url: str) -> ProbeResponse:
        """OPTIONS preflight asking to send ``GET`` with a ``Range`` header."""


@dataclass(frozen=True)
class _Target:
    """One probeable asset: where it is declared and what size it claims."""

    node: Node
    key: str
    url: str
    declared_size: int | None


@dataclass(frozen=True)
class _LinkTarget:
    """One probeable link target: the first link, in path order, naming its URL."""

    node: Node
    index: int
    rel: object
    href: str
    url: str


def _lower_headers(items: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in items}


def _open(request: Request) -> Any:
    """Send ``request`` through ``rashid._store``'s probe opener.

    It reads the same TLS options as every other transport, and it answers a
    redirect off https as the 3xx it is instead of following it.
    """
    return _store.probe_opener().open(request, timeout=_TIMEOUT)


def _request(url: str, method: str, headers: dict[str, str]) -> ProbeResponse:
    from urllib.request import Request

    if urlparse(url).scheme.lower() != "https":
        raise ValueError(f"refusing to probe non-https URL: {url!r}")
    # Every probe shape — ranged GET, HEAD, OPTIONS preflight — funnels through
    # here, so setting the agent once covers all of them.
    request = Request(url, method=method, headers={"User-Agent": user_agent(), **headers})
    try:
        with _open(request) as response:
            return ProbeResponse(
                status=response.status, headers=_lower_headers(response.headers.items())
            )
    except HTTPError as exc:
        # A 4xx/5xx still carries the verdict (e.g. a rejected preflight), and
        # a redirect off https carries the URL it names: report the status and
        # headers rather than treating them as a transport failure. Only
        # network-level errors propagate.
        with exc:
            return ProbeResponse(status=exc.code, headers=_lower_headers(exc.headers.items()))


class _DefaultProber:
    """The default prober, https only.

    The ranged GET and the preflight run once per host and read response
    headers that obstore does not return, so they go out over urllib. The HEADs
    run once per asset and per link, so they go out over obstore, which reuses
    one connection pool per host. obstore answers a HEAD with the size alone.

    When the host answers but obstore does not return the answer, the HEAD is
    sent again over urllib, so the checks read the full response exactly as
    before. That covers a non-2xx status, whose headers ``_check_head`` still
    reads, a missing ``Content-Length``, and a redirect off https, which comes
    back as the 3xx itself and is never followed. A transport
    failure is not sent again: it marks the host dead, and asking twice only
    doubles the wait.
    """

    def get_range(self, url: str) -> ProbeResponse:
        return _request(url, "GET", {"Range": "bytes=0-0", "Origin": _PROBE_ORIGIN})

    def head(self, url: str) -> ProbeResponse:
        try:
            size = _store.head_size(url)
        except _store.StoreError as exc:
            if exc.status is None and not exc.refused:
                raise
            return _request(url, "HEAD", {"Origin": _PROBE_ORIGIN})
        return ProbeResponse(status=200, headers={"content-length": str(size)})

    def preflight(self, url: str) -> ProbeResponse:
        return _request(
            url,
            "OPTIONS",
            {
                "Origin": _PROBE_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": ", ".join(_REQUIRED_REQUEST),
            },
        )


def _targets_by_host(graph: CatalogGraph, base_url: str | None = None) -> dict[str, list[_Target]]:
    """Probeable assets grouped by host: absolute ``https`` hrefs as declared,
    plus — when ``base_url`` is given — relative hrefs resolved against it.

    ``base_url`` also names the publish host, which is what scopes the set to
    the catalog's own assets under PORTO-CORE-073: an absolute href on another
    host is a copy someone else serves, and dropping it here keeps every probe
    off upstream servers.

    Node iteration is path-sorted and asset keys are sorted, so each host's
    first target — the probe representative — is deterministic.
    """
    base = _normalize_base(base_url) if base_url is not None else None
    by_host: dict[str, list[_Target]] = {}
    for node in graph.iter(*_LIVE_KINDS):
        if node.parse_error is not None:
            continue
        assets = node.data.get("assets")
        if not isinstance(assets, dict):
            continue
        for key in sorted(assets):
            asset = assets[key]
            if not isinstance(asset, dict):
                continue
            href = asset.get("href")
            if not isinstance(href, str):
                continue
            url = _own_url(graph, node, href, base)
            if url is None:
                continue
            size = asset.get("file:size")
            by_host.setdefault(urlparse(url).netloc.lower(), []).append(
                _Target(
                    node=node,
                    key=key,
                    url=url,
                    declared_size=size if isinstance(size, int) else None,
                )
            )
    return by_host


def _own_url(graph: CatalogGraph, node: Node, href: str, base: str | None) -> str | None:
    """The URL a href on ``node`` is served from, or None when it is out of scope.

    An absolute ``https`` href is probed as declared, except that once ``base``
    (already normalized) names the publish host, an href on any other host is a
    copy someone else serves: core.md's hosting MUSTs do not reach it
    (PORTO-CORE-073), whatever roles it carries. A relative href joins onto
    ``base``; without one it cannot be placed. Absolute non-https hrefs and
    hrefs escaping the tree are never probed.
    """
    parsed = urlparse(href)
    if parsed.scheme.lower() == "https" and parsed.netloc:
        if base is not None and parsed.netloc.lower() != urlparse(base).netloc.lower():
            return None
        return href
    if base is None:
        return None
    rel = graph.resolve_path(node, href)
    if rel is None:
        return None
    return wire_url(base, str(rel))


def _link_targets(graph: CatalogGraph, base: str) -> list[_LinkTarget]:
    """Every distinct link URL under the publish base, sorted by URL.

    Where several links across the graph name one URL — ``root`` from every
    object, ``parent`` from every sibling — the first in path order stands for
    it, so a failure is reported once, against one document and pointer.
    """
    first: dict[str, _LinkTarget] = {}
    for node in graph.iter(*_LINK_KINDS):
        if node.parse_error is not None:
            continue
        for index, link in enumerate(links_of(node)):
            href = link.get("href")
            if not isinstance(href, str) or not href:
                continue
            url = _own_url(graph, node, href, base)
            if url is None or url in first:
                continue
            first[url] = _LinkTarget(
                node=node, index=index, rel=link.get("rel"), href=href, url=url
            )
    return [first[url] for url in sorted(first)]


def _normalize_base(base_url: str) -> str:
    """Validate and normalize a publish base URL to https with one trailing slash.

    https-only for the same reason the data reader is: the base joins with
    catalog-controlled hrefs into request URLs, so any weaker scheme would let
    a hostile tree downgrade or redirect the probes.
    """
    parsed = urlparse(base_url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"live base_url must be an https URL, got: {base_url!r}")
    return base_url.rstrip("/") + "/"


def _header_set(value: str | None) -> set[str]:
    """A comma-separated header value as a lowercased set of tokens."""
    if value is None:
        return set()
    return {token.strip().lower() for token in value.split(",") if token.strip()}


def _server_finding(
    rule_id: str,
    host: str,
    rep: _Target,
    message: str,
    fix_hint: str | None = None,
    expected: object | None = None,
    actual: object | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=f"host '{host}': {message}",
        path=str(rep.node.path),
        object_id=rep.node.id,
        json_pointer=f"/assets/{rep.key}/href",
        fix_hint=fix_hint,
        expected=expected,
        actual=actual,
    )


def _off_https_finding(
    node: Node, pointer: str, subject: str, probe: str, url: str, location: str
) -> Finding:
    return Finding(
        rule_id=LIV_OFF_HTTPS,
        severity=Severity.ERROR,
        message=f"{subject}: {probe} {url} redirected off https to {location}",
        path=str(node.path),
        object_id=node.id,
        json_pointer=pointer,
        fix_hint="serve the object over https, and redirect only to https URLs",
        expected="https",
        actual=location,
    )


def _check_range(host: str, rep: _Target, response: ProbeResponse) -> list[Finding]:
    problems: list[str] = []
    if response.status != 206:
        problems.append(f"a ranged GET returned {response.status}, expected 206 Partial Content")
    if (response.header("accept-ranges") or "").lower() != "bytes":
        problems.append("no 'Accept-Ranges: bytes' header")
    if not problems:
        return []
    return [
        _server_finding(
            LIV_RANGE,
            host,
            rep,
            "range requests unsupported: " + "; ".join(problems),
            fix_hint="serve assets from storage that honors the Range header with 206 responses",
            expected=206,
            actual=response.status,
        )
    ]


def _check_cors(host: str, rep: _Target, response: ProbeResponse) -> list[Finding]:
    if response.header("access-control-allow-origin") is None:
        # CORS is off entirely; the exposed-headers MUST is subsumed — one
        # finding for the root cause, not two for the same absent policy.
        return [
            _server_finding(
                LIV_CORS_ORIGIN,
                host,
                rep,
                "no Access-Control-Allow-Origin on a GET carrying an Origin header",
                fix_hint="enable a read-permitting CORS policy, e.g. Access-Control-Allow-Origin: *",
            )
        ]
    exposed = _header_set(response.header("access-control-expose-headers"))
    if "*" in exposed:
        return []
    missing = [name for name in _REQUIRED_EXPOSED if name.lower() not in exposed]
    if not missing:
        return []
    return [
        _server_finding(
            LIV_CORS_EXPOSE,
            host,
            rep,
            "Access-Control-Expose-Headers omits " + ", ".join(missing),
            fix_hint="expose " + ", ".join(_REQUIRED_EXPOSED) + " to browsers",
        )
    ]


def _check_preflight(host: str, rep: _Target, response: ProbeResponse) -> list[Finding]:
    problems: list[str] = []
    if response.status >= 400:
        problems.append(f"preflight returned {response.status}")
    methods = _header_set(response.header("access-control-allow-methods"))
    if "*" not in methods:
        missing = [m for m in ("GET", "HEAD") if m.lower() not in methods]
        if missing:
            problems.append("allowed methods omit " + ", ".join(missing))
    allowed = _header_set(response.header("access-control-allow-headers"))
    if "*" not in allowed:
        absent = [h for h in _REQUIRED_REQUEST if h.lower() not in allowed]
        if absent:
            problems.append("allowed request headers omit " + ", ".join(absent))
    if not problems:
        return []
    return [
        _server_finding(
            LIV_CORS_PREFLIGHT,
            host,
            rep,
            "CORS preflight failed: " + "; ".join(problems),
            fix_hint=(
                "allow the GET and HEAD methods and the "
                + ", ".join(_REQUIRED_REQUEST)
                + " request headers in the CORS policy"
            ),
        )
    ]


def _check_head(target: _Target, response: ProbeResponse) -> list[Finding]:
    length = response.header("content-length")
    pointer = f"/assets/{target.key}"
    if length is None or not length.isdigit():
        return [
            Finding(
                rule_id=LIV_HEAD_LENGTH,
                severity=Severity.ERROR,
                message=f"asset '{target.key}': HEAD returned no usable Content-Length",
                path=str(target.node.path),
                object_id=target.node.id,
                json_pointer=pointer,
                fix_hint="HEAD requests MUST return an accurate Content-Length",
                actual=length,
            )
        ]
    if target.declared_size is not None and int(length) != target.declared_size:
        return [
            Finding(
                rule_id=LIV_HEAD_LENGTH,
                severity=Severity.ERROR,
                message=(
                    f"asset '{target.key}': HEAD Content-Length {length} does not match "
                    f"the declared file:size {target.declared_size}"
                ),
                path=str(target.node.path),
                object_id=target.node.id,
                json_pointer=pointer,
                fix_hint="regenerate file:size at publish time so it matches the hosted bytes",
                expected=int(length),
                actual=target.declared_size,
            )
        ]
    return []


class _HeadCache:
    """One HEAD per distinct URL, shared by the asset and link checks.

    A link target that is also an asset (a relative ``pmtiles`` link, say) is
    then asked for once. A transport failure marks the host dead so no later
    check keeps hammering it: :meth:`head` returns None once the host failed,
    and the failure is reported once by whichever check hit it.

    ``known`` seeds the cache with statuses the caller already holds. A crawl
    that fetched a document by GET knows whether it exists, and asking the
    host again by HEAD would double the requests for nothing. A known status
    answers :meth:`status` only: it carries no headers, so it never stands in
    for an asset HEAD, whose ``Content-Length`` check needs the real response.
    """

    def __init__(self, prober: Prober, known: Mapping[str, int] | None = None) -> None:
        self._prober = prober
        self._responses: dict[str, ProbeResponse] = {}
        self._known: dict[str, int] = dict(known or {})
        self.failed: dict[str, Exception] = {}  # host -> the first transport error
        self._errors: dict[str, Exception] = {}  # url -> its prefetch's transport error

    def prefetch(self, host: str, urls: list[str]) -> None:
        """HEAD ``urls``, all on ``host``, in parallel, ahead of the checks that read them.

        The first URL goes alone. When it fails, the host is dead and the rest
        are not sent, as a sequential pass would not send them. A failure in
        the parallel batch is kept per URL, and :meth:`head` turns it into the
        host's failure when the checks reach that URL in order, so the
        findings do not depend on which request finished first.
        """
        pending = [
            u for u in dict.fromkeys(urls) if u not in self._responses and u not in self._known
        ]
        if not pending or self.head(pending[0]) is None:
            return
        rest = pending[1:]
        if host in self.failed or not rest:
            return
        log.debug("HEAD %d URL(s) on %s in parallel", len(rest), host)
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            outcomes = _trace.pool_map(pool, self._try_head, rest)
        for url, outcome in zip(rest, outcomes, strict=True):
            if isinstance(outcome, ProbeResponse):
                self._responses[url] = outcome
            else:
                self._errors[url] = outcome

    def _try_head(self, url: str) -> ProbeResponse | Exception:
        _trace.count("live.heads")
        try:
            return self._prober.head(url)
        except Exception as exc:  # noqa: BLE001 - kept for head() to report in order
            return exc

    def head(self, url: str) -> ProbeResponse | None:
        response = self._responses.get(url)
        if response is not None:
            _trace.count("live.head_cache_hits")
            return response
        host = urlparse(url).netloc.lower()
        if host in self.failed:
            return None
        error = self._errors.pop(url, None)
        if error is None:
            outcome = self._try_head(url)
            if isinstance(outcome, ProbeResponse):
                self._responses[url] = outcome
                return outcome
            error = outcome
        self.failed[host] = error
        return None

    def response(self, url: str) -> ProbeResponse | None:
        """The HEAD response already held for ``url``, without sending one."""
        return self._responses.get(url)

    def status(self, url: str) -> int | None:
        """The status the host answers for ``url``; None once the host failed."""
        known = self._known.get(url)
        if known is not None:
            _trace.count("live.known_statuses")
            return known
        response = self.head(url)
        return None if response is None else response.status


def _unavailable(message: str, path: str) -> Finding:
    return Finding(rule_id=LIV_UNAVAILABLE, severity=Severity.WARNING, message=message, path=path)


def _check_heads(
    host: str, targets: list[_Target], heads: _HeadCache, reported: _Target | None = None
) -> list[Finding]:
    """HEAD each distinct URL once, but check EVERY target against it.

    Two assets may share one URL while disagreeing on ``file:size`` — at most
    one of them can be right, so the response is cached per URL and the check
    still runs per asset.

    A HEAD that redirects off https is ``PTL-LIV-008`` for that asset, and its
    length is not checked: the 3xx carries the redirect's length, not the
    object's. ``reported`` is the asset whose redirect the ranged GET already
    reported, so its HEAD adds no second finding.
    """
    heads.prefetch(host, [target.url for target in targets])
    findings: list[Finding] = []
    for target in targets:
        response = heads.head(target.url)
        if response is None:
            findings.append(
                _unavailable(
                    f"HEAD probes against host '{host}' failed: {heads.failed[host]}",
                    str(target.node.path),
                )
            )
            break
        location = response.off_https()
        if location is None:
            findings.extend(_check_head(target, response))
        elif target is not reported:
            findings.append(
                _off_https_finding(
                    target.node,
                    f"/assets/{target.key}/href",
                    f"asset '{target.key}'",
                    "HEAD",
                    target.url,
                    location,
                )
            )
    return findings


def _probe_host(
    host: str, rep: _Target, prober: Prober, heads: _HeadCache
) -> tuple[ProbeResponse, ProbeResponse] | Finding:
    """The ranged GET and the preflight of ``rep``, or the warning that the host failed."""
    log.debug("probing host %s: range and CORS via %s", host, rep.url)
    try:
        _trace.count("live.range_probes")
        ranged = prober.get_range(rep.url)
        _trace.count("live.preflights")
        preflighted = prober.preflight(rep.url)
    except Exception as exc:  # noqa: BLE001 - an unreachable host is reported once
        log.debug("host %s failed: %s", host, exc)
        heads.failed.setdefault(host, exc)
        return _unavailable(f"live probes against host '{host}' failed: {exc}", str(rep.node.path))
    return ranged, preflighted


def _check_server(
    host: str, rep: _Target, ranged: ProbeResponse, preflighted: ProbeResponse
) -> list[Finding]:
    """The host-level range and CORS checks, read off ``rep``'s two probes."""
    findings = _check_range(host, rep, ranged)
    findings.extend(_check_cors(host, rep, ranged))
    findings.extend(_check_preflight(host, rep, preflighted))
    return findings


def _ranged_off_https(rep: _Target, location: str) -> Finding:
    return _off_https_finding(
        rep.node,
        f"/assets/{rep.key}/href",
        f"asset '{rep.key}'",
        "a ranged GET of",
        rep.url,
        location,
    )


def _check_host(
    host: str, targets: list[_Target], prober: Prober, heads: _HeadCache
) -> list[Finding]:
    """Range and CORS once per host, through one representative asset; then every HEAD.

    The representative is the host's first asset. When its ranged GET
    redirects off https, the 3xx says nothing about range or CORS support, so
    the redirect is reported and the HEADs pick the next representative: the
    first asset at another URL whose HEAD stays on https. When no asset
    qualifies, or its ranged GET redirects off https too, ``PTL-LIV-000``
    says the host-level checks did not run.
    """
    first = targets[0]
    log.debug("probing host %s: %d target(s)", host, len(targets))
    probed = _probe_host(host, first, prober, heads)
    if isinstance(probed, Finding):
        return [probed]
    ranged, preflighted = probed
    location = ranged.off_https()
    if location is None:
        findings = _check_server(host, first, ranged, preflighted)
        findings.extend(_check_heads(host, targets, heads))
        return findings
    findings = [_ranged_off_https(first, location)]
    head_findings = _check_heads(host, targets, heads, reported=first)
    if host in heads.failed:
        return findings + head_findings
    rep = next((t for t in targets if t.url != first.url and _stays_on_https(heads, t.url)), None)
    skipped = "every asset on it redirects off https"
    if rep is not None:
        probed = _probe_host(host, rep, prober, heads)
        if isinstance(probed, Finding):
            return [*findings, probed, *head_findings]
        ranged, preflighted = probed
        location = ranged.off_https()
        if location is None:
            findings.extend(_check_server(host, rep, ranged, preflighted))
            return findings + head_findings
        findings.append(_ranged_off_https(rep, location))
        skipped = "the ranged GET of every asset tried redirects off https"
    findings.append(
        _unavailable(
            f"range and CORS probes against host '{host}' did not run: {skipped}",
            str(first.node.path),
        )
    )
    return findings + head_findings


def _stays_on_https(heads: _HeadCache, url: str) -> bool:
    """Whether the HEAD already held for ``url`` answered without a redirect off https."""
    response = heads.response(url)
    return response is not None and response.off_https() is None


def _check_links(host: str, targets: list[_LinkTarget], heads: _HeadCache) -> list[Finding]:
    """HEAD every distinct link URL on the publish host; any non-2xx is an error.

    HEAD is a MUST for this host (PORTO-CORE-043), so a 405 is reported as the
    status it is rather than retried as GET. A host that already failed in
    transport during the asset probes was reported there and is not asked
    again; one that fails here is reported once as ``PTL-LIV-000``.
    """
    if host in heads.failed:
        return []
    heads.prefetch(host, [target.url for target in targets])
    findings: list[Finding] = []
    for target in targets:
        status = heads.status(target.url)
        if status is None:
            findings.append(
                _unavailable(
                    f"HEAD probes for link targets on host '{host}' failed: {heads.failed[host]}",
                    str(target.node.path),
                )
            )
            break
        if 200 <= status < 300:
            continue
        held = heads.response(target.url)
        location = None if held is None else held.off_https()
        if location is not None:
            findings.append(
                _off_https_finding(
                    target.node,
                    f"/links/{target.index}/href",
                    f"link rel:{target.rel!r} href '{target.href}'",
                    "HEAD",
                    target.url,
                    location,
                )
            )
            continue
        findings.append(
            Finding(
                rule_id=LIV_LINK_TARGET,
                severity=Severity.ERROR,
                message=(
                    f"link rel:{target.rel!r} href '{target.href}': HEAD {target.url}"
                    f" returned {status}, expected 2xx"
                ),
                path=str(target.node.path),
                object_id=target.node.id,
                json_pointer=f"/links/{target.index}/href",
                fix_hint="upload the linked document to the publish host, or correct the href",
                expected="2xx",
                actual=status,
            )
        )
    return findings


def _check_self_base(graph: CatalogGraph, base: str) -> list[Finding]:
    """The root ``self`` link, when present, names the base the caller named.

    core.md, Links: a published root catalog SHOULD carry an absolute ``self``
    link (PORTO-CORE-081). It is the catalog's own statement of where it is
    served from, and the caller's ``base`` is where it actually is. When they
    disagree, either the publish step wrote the tree to the wrong location or
    the ``self`` link was never updated; every client that trusts the link then
    resolves the tree's absolute hrefs against the wrong host. The two are
    compared as URLs, not strings: host case, a default port, and a ``./``
    segment do not make two spellings of one location disagree.
    """
    root = graph.root
    claimed = graph.published_base()
    if root is None or claimed is None:
        return []
    if _same_location(claimed, base):
        return []
    index = next(
        (
            i
            for i, link in enumerate(links_of(root))
            if link.get("rel") == "self" and is_absolute_href(str(link.get("href", "")))
        ),
        0,
    )
    return [
        Finding(
            rule_id=LIV_SELF_BASE,
            severity=Severity.WARNING,
            message=(
                f"root self link places the catalog under {claimed} but it is"
                f" published under {base}"
            ),
            path=str(root.path),
            object_id=root.id,
            json_pointer=f"/links/{index}/href",
            fix_hint=(
                f"set the root self link to {base}{root.path.name}, or publish"
                " the catalog at the location the self link names"
            ),
            expected=base,
            actual=claimed,
        )
    ]


def _same_location(left: str, right: str) -> bool:
    """True when two base URLs name one location, whatever their spelling."""
    return _location_key(left) == _location_key(right)


def _location_key(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    port = parsed.port
    default = {"https": 443, "http": 80}.get(scheme)
    netloc = host if port is None or port == default else f"{host}:{port}"
    path = posixpath.normpath(parsed.path or "/")
    return scheme, netloc, path.rstrip("/") + "/"


def _resolve_base(graph: CatalogGraph, base_url: str | None) -> tuple[str | None, bool]:
    """The publish base to probe under, and whether the caller supplied it.

    The caller's ``base_url`` wins, then the base recorded on the graph (the
    URL ``rashid check`` was given), then the root ``self`` link
    (PORTO-CORE-081): a catalog that states its own published location needs
    no ``--live-base-url``. A ``self`` link that is not an https URL with a
    host is ignored rather than probed, for the reason :func:`_normalize_base`
    gives.
    """
    named = base_url if base_url is not None else graph.base_url
    if named is not None:
        return _normalize_base(named), True
    claimed = graph.published_base()
    if claimed is not None:
        parsed = urlparse(claimed)
        if parsed.scheme.lower() == "https" and parsed.netloc:
            return _normalize_base(claimed), False
    return None, False


def validate_live(
    graph: CatalogGraph,
    prober: Prober | None = None,
    *,
    base_url: str | None = None,
    known_statuses: Mapping[str, int] | None = None,
) -> list[Finding]:
    """Probe the hosts behind the catalog's assets, and the publish host behind
    its links.

    Absolute ``https`` hrefs are probed as declared. ``base_url`` — the https
    URL the catalog root is published under — additionally makes relative
    hrefs probeable by joining their root-relative paths onto it, and turns on
    the link-target check: one HEAD per distinct link URL under the base,
    ``PTL-LIV-006`` for each that does not answer 2xx. Without ``base_url`` the
    pass reads the base off the graph — the URL ``rashid check`` was given —
    or, failing that, off the root catalog's absolute ``self`` link
    (PORTO-CORE-081), so a catalog that states its own location needs no flag.
    When the caller names a base and the ``self`` link names a different one,
    ``PTL-LIV-007`` reports the disagreement.

    ``known_statuses`` maps URLs to statuses the caller already observed, so
    the link check does not re-ask a host about documents a crawl just
    fetched. Returns ``PTL-LIV-00x`` findings for each Data Storage MUST the
    hosting server violates. When the tree declares nothing probeable, or a
    host cannot be reached, the pass degrades to ``PTL-LIV-000`` warnings
    rather than failing the run.
    """
    if prober is None:
        prober = _DefaultProber()
    base, named = _resolve_base(graph, base_url)
    by_host = _targets_by_host(graph, base)
    findings: list[Finding] = []
    if not by_host:
        hint = (
            "asset probes skipped: no probeable asset hrefs"
            if base is not None
            else (
                "live pass skipped: no absolute https asset hrefs to probe"
                " (pass base_url, or give the root catalog an absolute self link,"
                " to probe relative hrefs)"
            )
        )
        findings.append(_unavailable(hint, "."))
        if base is None:
            return findings
    heads = _HeadCache(prober, known_statuses)
    for host in sorted(by_host):
        findings.extend(_check_host(host, by_host[host], prober, heads))
    if base is not None:
        base_host = urlparse(base).netloc.lower()
        findings.extend(_check_links(base_host, _link_targets(graph, base), heads))
        if named:
            findings.extend(_check_self_base(graph, base))
    return findings
