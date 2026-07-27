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
- an **OPTIONS preflight** (``Access-Control-Request-Method: GET``,
  ``Access-Control-Request-Headers: Range``) — allowed methods and request
  headers appear only on preflight responses, never on GET/HEAD.

Range and CORS semantics are server properties, so the GET and OPTIONS probes
run once per distinct host (via its lexically first asset); only the cheap HEAD
runs per asset. Only absolute ``https`` hrefs are probeable from a local tree —
relative hrefs would need a base URL mapping (a planned follow-up); ``s3`` and
friends are ``PTL-AST-002``'s domain. The pass is stdlib-only and lives in
core; like every optional pass it degrades to ``PTL-LIV-000`` warnings when it
cannot probe rather than failing the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from rashid.catalog import CatalogGraph, Kind, Node
from rashid.model import Finding, Severity

LIV_UNAVAILABLE = "PTL-LIV-000"
LIV_RANGE = "PTL-LIV-001"
LIV_HEAD_LENGTH = "PTL-LIV-002"
LIV_CORS_ORIGIN = "PTL-LIV-003"
LIV_CORS_EXPOSE = "PTL-LIV-004"
LIV_CORS_PREFLIGHT = "PTL-LIV-005"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
SPEC_IDS: dict[str, tuple[str, ...]] = {
    LIV_RANGE: ("PORTO-CORE-043",),
    LIV_HEAD_LENGTH: ("PORTO-CORE-043",),
    LIV_CORS_ORIGIN: ("PORTO-CORE-045",),
    LIV_CORS_EXPOSE: ("PORTO-CORE-045",),
    LIV_CORS_PREFLIGHT: ("PORTO-CORE-045",),
}

# Assets are declared on collections and items; catalogs carry none.
_LIVE_KINDS: tuple[Kind, ...] = ("collection", "item")

_TIMEOUT = 30  # seconds per request

# An arbitrary origin: a read-permitting CORS policy answers any origin, and a
# restrictive one will not match this, correctly failing the check.
_PROBE_ORIGIN = "https://rashid-live-probe.invalid"

# core.md, Data Storage — the response headers a server MUST expose to browsers.
_REQUIRED_EXPOSED = ("Content-Range", "Content-Length", "Accept-Ranges", "ETag")


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


def _lower_headers(items: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in items}


def _request(url: str, method: str, headers: dict[str, str]) -> ProbeResponse:
    if urlparse(url).scheme.lower() != "https":
        raise ValueError(f"refusing to probe non-https URL: {url!r}")
    request = Request(url, method=method, headers=headers)
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
                "Access-Control-Request-Headers": "Range",
            },
        )


def _targets_by_host(graph: CatalogGraph) -> dict[str, list[_Target]]:
    """Probeable assets (absolute ``https`` hrefs), grouped by host.

    Node iteration is path-sorted and asset keys are sorted, so each host's
    first target — the probe representative — is deterministic.
    """
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
            if _is_alternate(asset):
                # A source/alternate original lives on a server the publisher
                # does not control; the hosting MUSTs bind the servers hosting
                # the cloud-native primaries. Mirrors the data pass exemption.
                continue
            parsed = urlparse(href)
            if parsed.scheme.lower() != "https" or not parsed.netloc:
                continue
            size = asset.get("file:size")
            by_host.setdefault(parsed.netloc.lower(), []).append(
                _Target(
                    node=node,
                    key=key,
                    url=href,
                    declared_size=size if isinstance(size, int) else None,
                )
            )
    return by_host


def _is_alternate(asset: dict[str, Any]) -> bool:
    roles = asset.get("roles")
    if not isinstance(roles, list):
        return False
    return any(isinstance(role, str) and role in ("source", "alternate") for role in roles)


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
            fix_hint="expose Content-Range, Content-Length, Accept-Ranges, and ETag to browsers",
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
    if "*" not in allowed and "range" not in allowed:
        problems.append("allowed request headers omit Range")
    if not problems:
        return []
    return [
        _server_finding(
            LIV_CORS_PREFLIGHT,
            host,
            rep,
            "CORS preflight failed: " + "; ".join(problems),
            fix_hint="allow GET and HEAD methods and the Range request header in the CORS policy",
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


def _check_heads(host: str, targets: list[_Target], prober: Prober) -> list[Finding]:
    """HEAD each distinct URL once, but check EVERY target against it.

    Two assets may share one URL while disagreeing on ``file:size`` — at most
    one of them can be right, so the response is cached per URL and the check
    still runs per asset.
    """
    findings: list[Finding] = []
    responses: dict[str, ProbeResponse] = {}
    for target in targets:
        response = responses.get(target.url)
        if response is None:
            try:
                response = prober.head(target.url)
            except Exception as exc:  # noqa: BLE001 - a dead host is reported once
                findings.append(
                    Finding(
                        rule_id=LIV_UNAVAILABLE,
                        severity=Severity.WARNING,
                        message=f"HEAD probes against host '{host}' failed: {exc}",
                        path=str(target.node.path),
                    )
                )
                break
            responses[target.url] = response
        findings.extend(_check_head(target, response))
    return findings


def _check_host(host: str, targets: list[_Target], prober: Prober) -> list[Finding]:
    rep = targets[0]
    try:
        ranged = prober.get_range(rep.url)
        preflighted = prober.preflight(rep.url)
    except Exception as exc:  # noqa: BLE001 - an unreachable host is reported once
        return [
            Finding(
                rule_id=LIV_UNAVAILABLE,
                severity=Severity.WARNING,
                message=f"live probes against host '{host}' failed: {exc}",
                path=str(rep.node.path),
            )
        ]
    findings = _check_range(host, rep, ranged)
    findings.extend(_check_cors(host, rep, ranged))
    findings.extend(_check_preflight(host, rep, preflighted))
    findings.extend(_check_heads(host, targets, prober))
    return findings


def validate_live(graph: CatalogGraph, prober: Prober | None = None) -> list[Finding]:
    """Probe the hosts behind every absolute ``https`` asset href.

    Returns ``PTL-LIV-00x`` findings for each Data Storage MUST the hosting
    server violates. When the tree declares nothing probeable, or a host cannot
    be reached, the pass degrades to ``PTL-LIV-000`` warnings rather than
    failing the run.
    """
    if prober is None:
        prober = _UrllibProber()
    by_host = _targets_by_host(graph)
    if not by_host:
        return [
            Finding(
                rule_id=LIV_UNAVAILABLE,
                severity=Severity.WARNING,
                message=(
                    "live pass skipped: no absolute https asset hrefs to probe "
                    "(probing relative hrefs needs a base URL mapping, not yet supported)"
                ),
                path=".",
            )
        ]
    findings: list[Finding] = []
    for host in sorted(by_host):
        findings.extend(_check_host(host, by_host[host], prober))
    return findings
