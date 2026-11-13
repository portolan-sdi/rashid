"""Live-hosting pass: probe the publisher's servers for the Data Storage MUSTs —
HTTP range support and CORS (core.md, Data Storage).

The metadata and data passes read what is *in* the catalog; whether the hosting
server honors ``Range`` or lets a browser read across origins is a property of
the server itself, checkable only by probing it.

The range rules apply only to files with a partial-read media type (COG,
Parquet, PMTiles, COPC; PORTO-CORE-043, PORTO-CORE-045). CORS applies to every
metadata and asset file (PORTO-CORE-086). Per host, the probes are:

- a **ranged GET** (with an ``Origin`` header) of the first partial-read asset
  — ``206 Partial Content``, ``Accept-Ranges: bytes``,
  ``Access-Control-Allow-Origin``, and the exposed range headers. Servers omit
  every ``Access-Control-*`` header unless the request carries ``Origin``, so
  sending one is required, not optional. A host with no partial-read asset
  gets the same GET of its first asset, and only its allowed origin is read.
- an **OPTIONS preflight** of the same partial-read asset
  (``Access-Control-Request-Method: GET``, and ``Access-Control-Request-Headers``
  naming ``Range`` plus the conditional-request headers) — allowed request
  headers appear only on preflight responses, never on GET/HEAD. A failed
  preflight is an error, since no allowance can be read from it. GET and HEAD
  are CORS-safelisted methods, so ``Access-Control-Allow-Methods`` is not
  checked. A
  file read whole needs none: a browser sends a plain GET or HEAD without a
  preflight, so a host with no partial-read asset gets no OPTIONS.
- a **HEAD** per asset — an accurate ``Content-Length`` for a partial-read
  asset (checked for presence, and against the declared ``file:size`` when
  the asset carries one).

The section scopes its MUSTs to the servers hosting the publisher's own files
and exempts bytes a third party hosts (PORTO-CORE-073), so the target set is
built from who serves an asset rather than from the roles it carries. Given
``base_url``, an asset is the catalog's own when its href is relative (it lives
in the published tree) or absolute on the publish host; an absolute href on any
other host is upstream and is skipped. A census.gov ZIP that ignores ``Range``
says nothing about the catalog citing it. Without ``base_url`` there is no
publish host to compare against, so every absolute href is probed as declared
and the PORTO-CORE-073 carve-out cannot be applied — pass ``base_url`` for a
catalog that cites upstream copies.

Range and CORS semantics are server properties, so the GET and OPTIONS probes
run once per distinct host; only the cheap HEAD runs per asset. Absolute
``https`` hrefs are probed as declared; relative hrefs are probed when the
caller supplies ``base_url`` — the URL the catalog root is published under —
by joining the root-relative asset path onto it. ``s3`` and friends are
``PTL-AST-002``'s domain. The pass needs no geospatial stack and lives in core;
like every optional pass it degrades to ``PTL-LIV-000`` warnings when it cannot
probe rather than failing the run.

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
fallback on a 405: every metadata file MUST allow HEAD (PORTO-CORE-086), so a
host that rejects HEAD is reported, not worked around. A publish host that
serves metadata but no asset gets its CORS GET through the first link target
that answers 2xx.

No probe leaves https. A redirect to another https URL is followed, and the
checks read the answer at its end. A redirect to any other scheme is not
followed: a client that stays on https gets no object there, so the asset or
link is reported as ``PTL-LIV-008``, with the URL the redirect names. When
the host's first asset redirects that way, the range and CORS probes move to
the next asset of the same kind whose HEAD stays on https; when none does,
``PTL-LIV-000`` says they did not run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError

from rashid import _store, _trace
from rashid._href import (
    href_host,
    is_absolute_href,
    live_host_href,
    location_key,
    normalize_live_base,
    off_https_reference,
    resolve_live_base,
    wire_url,
)
from rashid.catalog import CatalogGraph, Kind, Node
from rashid.model import Finding, Severity, json_pointer
from rashid.registry import (
    LIV_CORS_EXPOSE,
    LIV_CORS_ORIGIN,
    LIV_CORS_PREFLIGHT,
    LIV_HEAD_LENGTH,
    LIV_LINK_TARGET,
    LIV_OFF_HTTPS,
    LIV_RANGE,
    LIV_SELF_BASE,
    LIV_UNAVAILABLE,
)
from rashid.rules._common import assets_of, indexed_links_of, is_partial_read_media_type

log = logging.getLogger(__name__)


# Assets are declared on collections and items; catalogs carry none.
_LIVE_KINDS: tuple[Kind, ...] = ("collection", "item")

# Links are declared on every object kind.
_LINK_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")

# Concurrent HEADs against one host.
_WORKERS = 32

# An arbitrary origin: a read-permitting CORS policy answers any origin, and a
# restrictive one will not match this, correctly failing the check.
_PROBE_ORIGIN = "https://rashid-live-probe.invalid"

# core.md, Data Storage — the response headers a server MUST expose to browsers
# for a partial-read file.
_REQUIRED_EXPOSED = (
    "Content-Range",
    "Accept-Ranges",
    "ETag",
)

# core.md, Data Storage — the request headers a server MUST allow for a
# partial-read file. The
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

    def access_denied(self) -> bool:
        """Only authentication denials make conformance probes inaccessible."""
        return self.status in (401, 403)

    def off_https(self) -> str | None:
        """The non-https URL this response redirects to; None for any other answer.

        A redirect that stays on https is followed before a response is
        built, so a 3xx seen here points off https or names no location. A
        relative ``Location`` keeps the request's https scheme.
        """
        if not 300 <= self.status < 400:
            return None
        location = self.header("location")
        if location is None or not off_https_reference(location):
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
    """One probeable file: where it is declared, and what the checks read off it.

    An asset, or a metadata document that stands in for a host with no asset.
    """

    node: Node
    key: str
    url: str
    declared_size: int | None
    partial_read: bool
    pointer: str  # the href that names the file
    subject: str  # how a message names it

    def server_finding(
        self,
        rule_id: str,
        *,
        message: str,
        fix_hint: str | None = None,
        expected: object | None = None,
        actual: object | None = None,
    ) -> Finding:
        """Report a hosting defect against this asset and its URL's host."""
        host = href_host(self.url)
        return Finding(
            rule_id=rule_id,
            severity=Severity.ERROR,
            message=f"host '{host}': {message}",
            path=str(self.node.path),
            object_id=self.node.id,
            json_pointer=self.pointer,
            fix_hint=fix_hint,
            expected=expected,
            actual=actual,
        )


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


def _probe_response(url: str, method: str, headers: dict[str, str]) -> ProbeResponse:
    try:
        with _store.probe(url, method, headers) as response:
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
    before. That covers a non-2xx status, which obstore reports as an error,
    a missing ``Content-Length``, and a redirect off https, which comes
    back as the 3xx itself and is never followed. A transport
    failure is not sent again: it marks the host dead, and asking twice only
    doubles the wait.
    """

    def get_range(self, url: str) -> ProbeResponse:
        return _probe_response(url, "GET", {"Range": "bytes=0-0", "Origin": _PROBE_ORIGIN})

    def head(self, url: str) -> ProbeResponse:
        try:
            size = _store.head_size(url)
        except _store.StoreError as exc:
            if exc.status is None and not exc.refused:
                raise
            return _probe_response(url, "HEAD", {"Origin": _PROBE_ORIGIN})
        return ProbeResponse(status=200, headers={"content-length": str(size)})

    def preflight(self, url: str) -> ProbeResponse:
        return _probe_response(
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
    base = normalize_live_base(base_url) if base_url is not None else None
    by_host: dict[str, list[_Target]] = {}
    for node in graph.iter(*_LIVE_KINDS):
        if node.parse_error is not None:
            continue
        for _pointer, key, asset in sorted(assets_of(node), key=lambda entry: entry[1]):
            href = asset.get("href")
            if not isinstance(href, str):
                continue
            url = live_host_href(str(graph.href_path(node).parent), href, base)
            if url is None:
                continue
            if not is_absolute_href(href):
                url = wire_url("", url)
            size = asset.get("file:size")
            by_host.setdefault(href_host(url), []).append(
                _Target(
                    node=node,
                    key=key,
                    url=url,
                    declared_size=size if isinstance(size, int) else None,
                    partial_read=is_partial_read_media_type(asset.get("type")),
                    pointer=json_pointer("assets", key, "href"),
                    subject=f"asset '{key}'",
                )
            )
    return by_host


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
        for index, link in indexed_links_of(node):
            href = link.get("href")
            if not isinstance(href, str) or not href:
                continue
            url = live_host_href(str(graph.href_path(node).parent), href, base)
            if url is None:
                continue
            if not is_absolute_href(href):
                url = wire_url("", url)
            if url in first:
                continue
            first[url] = _LinkTarget(
                node=node, index=index, rel=link.get("rel"), href=href, url=url
            )
    return [first[url] for url in sorted(first)]


def _header_set(value: str | None) -> set[str]:
    """A comma-separated header value as a lowercased set of tokens."""
    if value is None:
        return set()
    return {token.strip().lower() for token in value.split(",") if token.strip()}


@dataclass(frozen=True)
class _ProbeRedirect:
    """A probe request and the non-https location its response names."""

    probe: str
    url: str
    location: str


def _off_https_finding(
    node: Node, *, pointer: str, subject: str, result: _ProbeRedirect
) -> Finding:
    return Finding(
        rule_id=LIV_OFF_HTTPS,
        severity=Severity.ERROR,
        message=f"{subject}: {result.probe} {result.url} redirected off https to {result.location}",
        path=str(node.path),
        object_id=node.id,
        json_pointer=pointer,
        fix_hint="serve the object over https, and redirect only to https URLs",
        expected="https",
        actual=result.location,
    )


def _check_range(rep: _Target, response: ProbeResponse) -> list[Finding]:
    problems: list[str] = []
    if response.status != 206:
        problems.append(f"a ranged GET returned {response.status}, expected 206 Partial Content")
    if (response.header("accept-ranges") or "").lower() != "bytes":
        problems.append("no 'Accept-Ranges: bytes' header")
    if not problems:
        return []
    return [
        rep.server_finding(
            LIV_RANGE,
            message="range requests unsupported: " + "; ".join(problems),
            fix_hint="serve assets from storage that honors the Range header with 206 responses",
            expected=206,
            actual=response.status,
        )
    ]


def _check_cors(rep: _Target, response: ProbeResponse) -> list[Finding]:
    if response.header("access-control-allow-origin") is None:
        # CORS is off entirely; the exposed-headers MUST is subsumed — one
        # finding for the root cause, not two for the same absent policy.
        return [
            rep.server_finding(
                LIV_CORS_ORIGIN,
                message="no Access-Control-Allow-Origin on a GET carrying an Origin header",
                fix_hint="enable a read-permitting CORS policy, e.g. Access-Control-Allow-Origin: *",
            )
        ]
    exposed = _header_set(response.header("access-control-expose-headers"))
    if not rep.partial_read or "*" in exposed:
        return []
    missing = [name for name in _REQUIRED_EXPOSED if name.lower() not in exposed]
    if not missing:
        return []
    return [
        rep.server_finding(
            LIV_CORS_EXPOSE,
            message="Access-Control-Expose-Headers omits " + ", ".join(missing),
            fix_hint="expose " + ", ".join(_REQUIRED_EXPOSED) + " to browsers",
        )
    ]


def _check_preflight(rep: _Target, response: ProbeResponse) -> list[Finding]:
    problems: list[str] = []
    if response.status >= 400:
        problems.append(f"preflight returned {response.status}")
    allowed = _header_set(response.header("access-control-allow-headers"))
    if "*" not in allowed:
        absent = [h for h in _REQUIRED_REQUEST if h.lower() not in allowed]
        if absent:
            problems.append("allowed request headers omit " + ", ".join(absent))
    if not problems:
        return []
    return [
        rep.server_finding(
            LIV_CORS_PREFLIGHT,
            message="CORS preflight failed: " + "; ".join(problems),
            fix_hint=(
                "allow the " + ", ".join(_REQUIRED_REQUEST) + " request headers in the CORS policy"
            ),
        )
    ]


def _check_head(target: _Target, response: ProbeResponse) -> list[Finding]:
    if response.access_denied():
        return [
            _unavailable(
                f"asset '{target.key}': HEAD {target.url} returned {response.status}; "
                "asset inaccessible",
                str(target.node.path),
            )
        ]
    length = response.header("content-length")
    pointer = json_pointer("assets", target.key)
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
        except Exception as exc:  # kept for head() to report in order
            return exc

    def head(self, url: str) -> ProbeResponse | None:
        response = self._responses.get(url)
        if response is not None:
            _trace.count("live.head_cache_hits")
            return response
        host = href_host(url)
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
            if target.partial_read or response.access_denied():
                findings.extend(_check_head(target, response))
        elif target is not reported:
            findings.append(
                _off_https_finding(
                    target.node,
                    pointer=target.pointer,
                    subject=target.subject,
                    result=_ProbeRedirect("HEAD", target.url, location),
                )
            )
    return findings


def _probe_host(
    host: str, rep: _Target, prober: Prober, heads: _HeadCache
) -> tuple[ProbeResponse, ProbeResponse | None] | Finding:
    """The ranged GET of ``rep`` and, for a partial-read file, its preflight;
    or the warning that the host failed.

    A file read whole needs no preflight: a browser sends a plain GET or HEAD
    without one, and only ``Access-Control-Allow-Origin`` decides it.
    """
    log.debug("probing host %s: range and CORS via %s", host, rep.url)
    preflighted = None
    try:
        _trace.count("live.range_probes")
        ranged = prober.get_range(rep.url)
        if rep.partial_read:
            _trace.count("live.preflights")
            preflighted = prober.preflight(rep.url)
    except Exception as exc:  # an unreachable host is reported once
        log.debug("host %s failed: %s", host, exc)
        heads.failed.setdefault(host, exc)
        return _unavailable(f"live probes against host '{host}' failed: {exc}", str(rep.node.path))
    return ranged, preflighted


def _check_server(
    rep: _Target, ranged: ProbeResponse, preflighted: ProbeResponse | None
) -> list[Finding]:
    """The host-level range and CORS checks, read off ``rep``'s probes."""
    if ranged.access_denied():
        findings = [
            _unavailable(
                f"range and CORS probes for {rep.url} returned {ranged.status}; asset inaccessible",
                str(rep.node.path),
            )
        ]
    else:
        findings = _check_range(rep, ranged) if rep.partial_read else []
        findings.extend(_check_cors(rep, ranged))
    if preflighted is not None:
        findings.extend(_check_preflight(rep, preflighted))
    return findings


def _ranged_off_https(rep: _Target, location: str) -> Finding:
    return _off_https_finding(
        rep.node,
        pointer=rep.pointer,
        subject=rep.subject,
        result=_ProbeRedirect("a ranged GET of", rep.url, location),
    )


def _check_host(
    host: str, targets: list[_Target], prober: Prober, heads: _HeadCache
) -> list[Finding]:
    """Range and CORS once per host, through one representative asset; then every HEAD.

    The representative is the host's first partial-read asset, or its first
    asset when it has none. When its ranged GET redirects off https, the 3xx
    says nothing about range or CORS support, so the redirect is reported and
    the HEADs pick the next representative of the same kind: the first at
    another URL whose HEAD stays on https. When no partial-read asset
    qualifies, the first other asset that does still gets the CORS check.
    ``PTL-LIV-000`` names the checks that did not run.
    """
    candidates = [t for t in targets if t.partial_read] or targets
    first = candidates[0]
    log.debug("probing host %s: %d target(s)", host, len(targets))
    probed = _probe_host(host, first, prober, heads)
    if isinstance(probed, Finding):
        return [probed]
    ranged, preflighted = probed
    location = ranged.off_https()
    if location is None:
        findings = _check_server(first, ranged, preflighted)
        findings.extend(_check_heads(host, targets, heads))
        return findings
    findings = [_ranged_off_https(first, location)]
    head_findings = _check_heads(host, targets, heads, reported=first)
    if host in heads.failed:
        return findings + head_findings
    # Without a partial-read stand-in, a file read whole still carries the CORS check.
    whole = [t for t in targets if not t.partial_read] if first.partial_read else []
    skipped = f"every {'partial-read ' if whole else ''}asset on it redirects off https"
    what = "range and CORS probes"
    for pool in (candidates, whole):
        rep = next((t for t in pool if t.url != first.url and _stays_on_https(heads, t.url)), None)
        if rep is None:
            continue
        probed = _probe_host(host, rep, prober, heads)
        if isinstance(probed, Finding):
            return [*findings, probed, *head_findings]
        ranged, preflighted = probed
        location = ranged.off_https()
        if location is not None:
            findings.append(_ranged_off_https(rep, location))
            skipped = "the ranged GET of every asset tried redirects off https"
            continue
        findings.extend(_check_server(rep, ranged, preflighted))
        if rep.partial_read == first.partial_read:
            return findings + head_findings
        what = "range probes"
        break
    findings.append(
        _unavailable(
            f"{what} against host '{host}' did not run: {skipped}",
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

    Every metadata file MUST allow HEAD (PORTO-CORE-086), so a 405 is reported
    as the status it is rather than retried as GET. A host that already failed in
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
                    pointer=json_pointer("links", target.index, "href"),
                    subject=f"link rel:{target.rel!r} href '{target.href}'",
                    result=_ProbeRedirect("HEAD", target.url, location),
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
                json_pointer=json_pointer("links", target.index, "href"),
                fix_hint="upload the linked document to the publish host, or correct the href",
                expected="2xx",
                actual=status,
            )
        )
    return findings


def _check_metadata_host(
    host: str, targets: list[_LinkTarget], prober: Prober, heads: _HeadCache
) -> list[Finding]:
    """CORS on a publish host that serves no asset, through its first link target
    that answers 2xx (PORTO-CORE-086). The HEADs are already held, so only the
    one GET goes out.
    """
    link = next((t for t in targets if 200 <= (heads.status(t.url) or 0) < 300), None)
    if host in heads.failed or link is None:
        return []
    rep = _Target(
        node=link.node,
        key="",
        url=link.url,
        declared_size=None,
        partial_read=False,
        pointer=json_pointer("links", link.index, "href"),
        subject=f"link rel:{link.rel!r} href '{link.href}'",
    )
    probed = _probe_host(host, rep, prober, heads)
    if isinstance(probed, Finding):
        return [probed]
    ranged, _ = probed
    location = ranged.off_https()
    if location is not None:
        return [_ranged_off_https(rep, location)]
    return _check_server(rep, ranged, None)


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
    claimed_location = location_key(claimed)
    if claimed_location is not None and claimed_location == location_key(base):
        return []
    index = next(
        (
            i
            for i, link in indexed_links_of(root)
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
            json_pointer=json_pointer("links", index, "href"),
            fix_hint=(
                f"set the root self link to {base}{root.path.name}, or publish"
                " the catalog at the location the self link names"
            ),
            expected=base,
            actual=claimed,
        )
    ]


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
    base, named = resolve_live_base(base_url, graph.base_url, graph.published_base())
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
        base_host = href_host(base)
        links = _link_targets(graph, base)
        findings.extend(_check_links(base_host, links, heads))
        if base_host not in by_host:
            findings.extend(_check_metadata_host(base_host, links, prober, heads))
        if named:
            findings.extend(_check_self_base(graph, base))
    return findings
