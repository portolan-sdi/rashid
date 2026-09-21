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
``s3`` and friends are ``PTL-AST-002``'s domain. The pass is stdlib-only and
lives in core; like every optional pass it degrades to ``PTL-LIV-000``
warnings when it cannot probe rather than failing the run.

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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rashid._http import user_agent
from rashid.catalog import CatalogGraph, Kind, Node
from rashid.model import Finding, Severity
from rashid.rules._common import links_of

LIV_UNAVAILABLE = "PTL-LIV-000"
LIV_RANGE = "PTL-LIV-001"
LIV_HEAD_LENGTH = "PTL-LIV-002"
LIV_CORS_ORIGIN = "PTL-LIV-003"
LIV_CORS_EXPOSE = "PTL-LIV-004"
LIV_CORS_PREFLIGHT = "PTL-LIV-005"
LIV_LINK_TARGET = "PTL-LIV-006"

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
}

# Assets are declared on collections and items; catalogs carry none.
_LIVE_KINDS: tuple[Kind, ...] = ("collection", "item")

# Links are declared on every object kind.
_LINK_KINDS: tuple[Kind, ...] = ("catalog", "collection", "item")

_TIMEOUT = 30  # seconds per request

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


class Prober(Protocol):
    """Issues the three probe requests against one URL."""

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


def _request(url: str, method: str, headers: dict[str, str]) -> ProbeResponse:
    if urlparse(url).scheme.lower() != "https":
        raise ValueError(f"refusing to probe non-https URL: {url!r}")
    # Every probe shape — ranged GET, HEAD, OPTIONS preflight — funnels through
    # here, so setting the agent once covers all of them.
    request = Request(url, method=method, headers={"User-Agent": user_agent(), **headers})
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310  # nosec B310
            return ProbeResponse(
                status=response.status, headers=_lower_headers(response.headers.items())
            )
    except HTTPError as exc:
        # A 4xx/5xx still carries the verdict (e.g. a rejected preflight):
        # report its status and headers rather than treating it as transport
        # failure — only network-level errors propagate.
        return ProbeResponse(status=exc.code, headers=_lower_headers(exc.headers.items()))


class _UrllibProber:
    """The default prober: stdlib urllib, https only."""

    def get_range(self, url: str) -> ProbeResponse:
        return _request(url, "GET", {"Range": "bytes=0-0", "Origin": _PROBE_ORIGIN})

    def head(self, url: str) -> ProbeResponse:
        return _request(url, "HEAD", {"Origin": _PROBE_ORIGIN})

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
    return f"{base}{rel}"


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
    """

    def __init__(self, prober: Prober) -> None:
        self._prober = prober
        self._responses: dict[str, ProbeResponse] = {}
        self.failed: dict[str, Exception] = {}  # host -> the first transport error

    def head(self, url: str) -> ProbeResponse | None:
        response = self._responses.get(url)
        if response is not None:
            return response
        host = urlparse(url).netloc.lower()
        if host in self.failed:
            return None
        try:
            response = self._prober.head(url)
        except Exception as exc:  # noqa: BLE001 - a dead host is reported once
            self.failed[host] = exc
            return None
        self._responses[url] = response
        return response


def _unavailable(message: str, path: str) -> Finding:
    return Finding(rule_id=LIV_UNAVAILABLE, severity=Severity.WARNING, message=message, path=path)


def _check_heads(host: str, targets: list[_Target], heads: _HeadCache) -> list[Finding]:
    """HEAD each distinct URL once, but check EVERY target against it.

    Two assets may share one URL while disagreeing on ``file:size`` — at most
    one of them can be right, so the response is cached per URL and the check
    still runs per asset.
    """
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
        findings.extend(_check_head(target, response))
    return findings


def _check_host(
    host: str, targets: list[_Target], prober: Prober, heads: _HeadCache
) -> list[Finding]:
    rep = targets[0]
    try:
        ranged = prober.get_range(rep.url)
        preflighted = prober.preflight(rep.url)
    except Exception as exc:  # noqa: BLE001 - an unreachable host is reported once
        heads.failed.setdefault(host, exc)
        return [
            _unavailable(f"live probes against host '{host}' failed: {exc}", str(rep.node.path))
        ]
    findings = _check_range(host, rep, ranged)
    findings.extend(_check_cors(host, rep, ranged))
    findings.extend(_check_preflight(host, rep, preflighted))
    findings.extend(_check_heads(host, targets, heads))
    return findings


def _check_links(host: str, targets: list[_LinkTarget], heads: _HeadCache) -> list[Finding]:
    """HEAD every distinct link URL on the publish host; any non-2xx is an error.

    HEAD is a MUST for this host (PORTO-CORE-043), so a 405 is reported as the
    status it is rather than retried as GET. A host that already failed in
    transport during the asset probes was reported there and is not asked
    again; one that fails here is reported once as ``PTL-LIV-000``.
    """
    if host in heads.failed:
        return []
    findings: list[Finding] = []
    for target in targets:
        response = heads.head(target.url)
        if response is None:
            findings.append(
                _unavailable(
                    f"HEAD probes for link targets on host '{host}' failed: {heads.failed[host]}",
                    str(target.node.path),
                )
            )
            break
        if 200 <= response.status < 300:
            continue
        findings.append(
            Finding(
                rule_id=LIV_LINK_TARGET,
                severity=Severity.ERROR,
                message=(
                    f"link rel:{target.rel!r} href '{target.href}': HEAD {target.url}"
                    f" returned {response.status}, expected 2xx"
                ),
                path=str(target.node.path),
                object_id=target.node.id,
                json_pointer=f"/links/{target.index}/href",
                fix_hint="upload the linked document to the publish host, or correct the href",
                expected="2xx",
                actual=response.status,
            )
        )
    return findings


def validate_live(
    graph: CatalogGraph, prober: Prober | None = None, *, base_url: str | None = None
) -> list[Finding]:
    """Probe the hosts behind the catalog's assets, and the publish host behind
    its links.

    Absolute ``https`` hrefs are probed as declared. ``base_url`` — the https
    URL the catalog root is published under — additionally makes relative
    hrefs probeable by joining their root-relative paths onto it, and turns on
    the link-target check: one HEAD per distinct link URL under the base,
    ``PTL-LIV-006`` for each that does not answer 2xx. Returns ``PTL-LIV-00x``
    findings for each Data Storage MUST the hosting server violates. When the
    tree declares nothing probeable, or a host cannot be reached, the pass
    degrades to ``PTL-LIV-000`` warnings rather than failing the run.
    """
    if prober is None:
        prober = _UrllibProber()
    base = _normalize_base(base_url) if base_url is not None else None
    by_host = _targets_by_host(graph, base)
    findings: list[Finding] = []
    if not by_host:
        hint = (
            "asset probes skipped: no probeable asset hrefs"
            if base is not None
            else (
                "live pass skipped: no absolute https asset hrefs to probe"
                " (pass base_url to probe relative hrefs)"
            )
        )
        findings.append(_unavailable(hint, "."))
        if base is None:
            return findings
    heads = _HeadCache(prober)
    for host in sorted(by_host):
        findings.extend(_check_host(host, by_host[host], prober, heads))
    if base is not None:
        base_host = urlparse(base).netloc.lower()
        findings.extend(_check_links(base_host, _link_targets(graph, base), heads))
    return findings
