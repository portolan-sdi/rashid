"""Live-hosting pass tests.

The default prober reaches the network, so every deterministic test injects a
fake ``Prober``; the real-HTTP path is covered by the ``network``-marked
integration test in ``tests/integration/test_live_remote.py``.
"""

from __future__ import annotations

import re
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.request import Request

import pytest

from rashid import RulesConfig, _store, validate, validate_live
from rashid import live as live_mod
from rashid.catalog import CatalogGraph
from rashid.live import (
    LIV_CORS_EXPOSE,
    LIV_CORS_ORIGIN,
    LIV_CORS_PREFLIGHT,
    LIV_HEAD_LENGTH,
    LIV_LINK_TARGET,
    LIV_OFF_HTTPS,
    LIV_RANGE,
    LIV_SELF_BASE,
    LIV_UNAVAILABLE,
    ProbeResponse,
    _DefaultProber,
)
from rashid.model import Severity
from tests._https import HttpsServer, Route
from tests.conftest import VALID_MULTIHASH, CatalogBuilder, mutate_json

pytestmark = pytest.mark.unit

_URL = "https://data.example.com/roads.parquet"


def _good_range(**overrides: str) -> ProbeResponse:
    headers = {
        "accept-ranges": "bytes",
        "content-length": "1",
        "access-control-allow-origin": "*",
        "access-control-expose-headers": (
            "Content-Type, Content-Length, Content-Range, Accept-Ranges, ETag"
        ),
    }
    headers.update(overrides)
    return ProbeResponse(status=206, headers=headers)


def _good_head(length: str = "1234") -> ProbeResponse:
    return ProbeResponse(status=200, headers={"content-length": length})


def _good_preflight(**overrides: str) -> ProbeResponse:
    headers = {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, HEAD",
        "access-control-allow-headers": (
            "Range, If-Match, If-Modified-Since, If-None-Match, If-Unmodified-Since"
        ),
    }
    headers.update(overrides)
    return ProbeResponse(status=204, headers=headers)


class FakeProber:
    """Canned responses per probe kind, recording every call."""

    def __init__(
        self,
        range_response: ProbeResponse | None = None,
        head_response: ProbeResponse | None = None,
        preflight_response: ProbeResponse | None = None,
    ) -> None:
        self.range_response = range_response or _good_range()
        self.head_response = head_response or _good_head()
        self.preflight_response = preflight_response or _good_preflight()
        self.range_calls: list[str] = []
        self.head_calls: list[str] = []
        self.preflight_calls: list[str] = []

    def get_range(self, url: str) -> ProbeResponse:
        self.range_calls.append(url)
        return self.range_response

    def head(self, url: str) -> ProbeResponse:
        self.head_calls.append(url)
        return self.head_response

    def preflight(self, url: str) -> ProbeResponse:
        self.preflight_calls.append(url)
        return self.preflight_response


def _remote_asset(url: str = _URL, size: int = 1234) -> dict[str, object]:
    return {
        "href": url,
        "type": "application/vnd.apache.parquet",
        "roles": ["data"],
        "file:size": size,
        "file:checksum": VALID_MULTIHASH,
    }


def _graph_with_remote(catalog: CatalogBuilder, **asset_kwargs: object) -> CatalogGraph:
    catalog.collection("roads", assets={"data": _remote_asset(**asset_kwargs)})  # type: ignore[arg-type]
    return CatalogGraph.load(catalog.write())


def _ids(findings: list) -> list[str]:
    return [f.rule_id for f in findings]


def test_all_good_yields_no_findings(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    assert validate_live(graph, FakeProber()) == []


def test_no_remote_assets_is_single_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")  # builder defaults: relative hrefs only
    findings = validate_live(CatalogGraph.load(catalog.write()), FakeProber())
    assert _ids(findings) == [LIV_UNAVAILABLE]
    assert findings[0].severity is Severity.WARNING
    assert "no absolute https asset hrefs" in findings[0].message


def test_non_206_is_range_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    prober = FakeProber(range_response=ProbeResponse(status=200, headers=_good_range().headers))
    findings = validate_live(graph, prober)
    assert LIV_RANGE in _ids(findings)
    finding = next(f for f in findings if f.rule_id == LIV_RANGE)
    assert finding.severity is Severity.ERROR
    assert "206" in finding.message
    assert "data.example.com" in finding.message


def test_missing_accept_ranges_is_range_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    prober = FakeProber(range_response=_good_range(**{"accept-ranges": ""}))
    findings = validate_live(graph, prober)
    assert LIV_RANGE in _ids(findings)
    assert "Accept-Ranges" in next(f.message for f in findings if f.rule_id == LIV_RANGE)


def test_missing_allow_origin_is_cors_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    response = _good_range()
    del response.headers["access-control-allow-origin"]
    findings = validate_live(graph, FakeProber(range_response=response))
    assert LIV_CORS_ORIGIN in _ids(findings)
    finding = next(f for f in findings if f.rule_id == LIV_CORS_ORIGIN)
    assert finding.severity is Severity.ERROR


def test_any_allow_origin_value_passes(catalog: CatalogBuilder) -> None:
    # A server that echoes the probe origin permitted an arbitrary origin: pass.
    graph = _graph_with_remote(catalog)
    response = _good_range(**{"access-control-allow-origin": "https://rashid-live-probe.invalid"})
    findings = validate_live(graph, FakeProber(range_response=response))
    assert LIV_CORS_ORIGIN not in _ids(findings)


def test_missing_expose_headers_lists_the_missing(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    response = _good_range(
        **{"access-control-expose-headers": "Content-Type, Content-Range, Content-Length"}
    )
    findings = validate_live(graph, FakeProber(range_response=response))
    assert LIV_CORS_EXPOSE in _ids(findings)
    message = next(f.message for f in findings if f.rule_id == LIV_CORS_EXPOSE)
    assert "ETag" in message
    assert "Accept-Ranges" in message
    assert "Content-Range" not in message  # present ones are not reported


def test_missing_content_type_exposure_is_an_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    response = _good_range(
        **{"access-control-expose-headers": "Content-Length, Content-Range, Accept-Ranges, ETag"}
    )
    findings = validate_live(graph, FakeProber(range_response=response))
    assert LIV_CORS_EXPOSE in _ids(findings)
    assert "Content-Type" in next(f.message for f in findings if f.rule_id == LIV_CORS_EXPOSE)


def test_wildcard_expose_headers_passes(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    response = _good_range(**{"access-control-expose-headers": "*"})
    findings = validate_live(graph, FakeProber(range_response=response))
    assert LIV_CORS_EXPOSE not in _ids(findings)


def test_preflight_missing_method_is_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    prober = FakeProber(
        preflight_response=_good_preflight(**{"access-control-allow-methods": "GET"})
    )
    findings = validate_live(graph, prober)
    assert LIV_CORS_PREFLIGHT in _ids(findings)
    assert "HEAD" in next(f.message for f in findings if f.rule_id == LIV_CORS_PREFLIGHT)


def test_preflight_missing_range_header_is_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    response = _good_preflight()
    del response.headers["access-control-allow-headers"]
    findings = validate_live(graph, FakeProber(preflight_response=response))
    assert LIV_CORS_PREFLIGHT in _ids(findings)
    assert "Range" in next(f.message for f in findings if f.rule_id == LIV_CORS_PREFLIGHT)


@pytest.mark.parametrize(
    "absent",
    ["If-Match", "If-Modified-Since", "If-None-Match", "If-Unmodified-Since"],
)
def test_preflight_missing_a_conditional_header_is_error(
    catalog: CatalogBuilder, absent: str
) -> None:
    graph = _graph_with_remote(catalog)
    kept = [
        h
        for h in ("Range", "If-Match", "If-Modified-Since", "If-None-Match", "If-Unmodified-Since")
        if h != absent
    ]
    response = _good_preflight(**{"access-control-allow-headers": ", ".join(kept)})
    findings = validate_live(graph, FakeProber(preflight_response=response))
    assert LIV_CORS_PREFLIGHT in _ids(findings)
    message = next(f.message for f in findings if f.rule_id == LIV_CORS_PREFLIGHT)
    assert absent in message
    assert "Range" not in message  # present ones are not reported


def test_wildcard_preflight_passes(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    response = _good_preflight(
        **{"access-control-allow-methods": "*", "access-control-allow-headers": "*"}
    )
    findings = validate_live(graph, FakeProber(preflight_response=response))
    assert LIV_CORS_PREFLIGHT not in _ids(findings)


def test_rejected_preflight_is_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    prober = FakeProber(preflight_response=ProbeResponse(status=403, headers={}))
    findings = validate_live(graph, prober)
    assert LIV_CORS_PREFLIGHT in _ids(findings)


def test_head_missing_content_length_is_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    prober = FakeProber(head_response=ProbeResponse(status=200, headers={}))
    findings = validate_live(graph, prober)
    assert LIV_HEAD_LENGTH in _ids(findings)
    finding = next(f for f in findings if f.rule_id == LIV_HEAD_LENGTH)
    assert finding.json_pointer == "/assets/data"
    assert finding.path == "roads/collection.json"


def test_head_length_mismatching_declared_size_is_error(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog, size=999)
    findings = validate_live(graph, FakeProber(head_response=_good_head("1234")))
    assert LIV_HEAD_LENGTH in _ids(findings)
    finding = next(f for f in findings if f.rule_id == LIV_HEAD_LENGTH)
    assert "999" in finding.message and "1234" in finding.message
    # structured mirror of the prose, data-pass convention: expected is the
    # authoritative value (the served bytes), actual is what the object declares
    assert finding.expected == 1234
    assert finding.actual == 999


def test_head_without_declared_size_only_requires_presence(catalog: CatalogBuilder) -> None:
    asset = _remote_asset()
    del asset["file:size"]
    catalog.collection("roads", assets={"data": asset})
    graph = CatalogGraph.load(catalog.write())
    findings = validate_live(graph, FakeProber(head_response=_good_head("777")))
    assert LIV_HEAD_LENGTH not in _ids(findings)


def test_same_host_probed_once_heads_per_asset(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={
            "data": _remote_asset("https://data.example.com/roads.parquet"),
            "pmtiles": _remote_asset("https://data.example.com/roads.pmtiles"),
        },
    )
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    assert validate_live(graph, prober) == []
    assert len(prober.range_calls) == 1
    assert len(prober.preflight_calls) == 1
    assert len(prober.head_calls) == 2


def test_distinct_hosts_each_probed(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={
            "a": _remote_asset("https://a.example.com/x.parquet"),
            "b": _remote_asset("https://b.example.com/y.parquet"),
        },
    )
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    assert validate_live(graph, prober) == []
    assert len(prober.range_calls) == 2
    assert len(prober.preflight_calls) == 2


def test_duplicate_url_headed_once(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={"a": _remote_asset(_URL), "b": _remote_asset(_URL)},
    )
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    validate_live(graph, prober)
    assert len(prober.head_calls) == 1


def test_duplicate_url_still_checks_every_declared_size(catalog: CatalogBuilder) -> None:
    # Two assets share one URL but disagree on file:size: at most one can be
    # right. The HEAD is deduplicated, the check is not — found by dogfooding
    # two collections pointing at the same hosted parquet.
    catalog.collection(
        "roads",
        assets={
            "right": _remote_asset(_URL, size=1234),
            "wrong": _remote_asset(_URL, size=42),
        },
    )
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber(head_response=_good_head("1234"))
    findings = validate_live(graph, prober)
    assert len(prober.head_calls) == 1
    assert _ids(findings) == [LIV_HEAD_LENGTH]
    assert findings[0].json_pointer == "/assets/wrong"


def test_upstream_hosts_are_not_probed(catalog: CatalogBuilder) -> None:
    # PORTO-CORE-073: the Data Storage MUSTs reach the servers hosting the
    # catalog's own cloud-native assets. census.gov serves an upstream original
    # and api.example.gov an unrelated export; neither is the publish host, so
    # neither is probed. What the assets are roled does not enter into it.
    source = _remote_asset("https://www2.census.gov/geo/tiger/counties.zip")
    source["roles"] = ["source"]
    unroled = _remote_asset("https://api.example.gov/export.geojson")
    catalog.collection("roads", assets={"source": source, "upstream": unroled})
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    findings = validate_live(graph, prober, base_url="https://data.example.org/cat")
    assert prober.range_calls == []
    # the remaining HEADs are the link-target probes, all on the publish host
    assert all(u.startswith("https://data.example.org/cat/") for u in prober.head_calls)
    assert _ids(findings) == [LIV_UNAVAILABLE]  # no asset is probeable


def test_absolute_href_on_the_publish_host_is_probed(catalog: CatalogBuilder) -> None:
    # The mirror image of the test above: writing the catalog's own asset as an
    # absolute URL rather than a relative path does not put it out of scope.
    own = _remote_asset("https://data.example.org/cat/roads/data.parquet")
    catalog.collection("roads", assets={"data": own})
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    findings = validate_live(graph, prober, base_url="https://data.example.org/cat")
    assert prober.range_calls == ["https://data.example.org/cat/roads/data.parquet"]
    assert "https://data.example.org/cat/roads/data.parquet" in prober.head_calls
    assert findings == []


def test_source_role_alone_does_not_exempt_from_probing(catalog: CatalogBuilder) -> None:
    # Hosting decides, not the role. Without base_url there is no publish host
    # to compare against, so an absolute href is probed as declared whatever it
    # carries — the documented limit of the carve-out.
    source = _remote_asset()
    source["roles"] = ["source"]
    catalog.collection("roads", assets={"data": source})
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    validate_live(graph, prober)
    assert prober.head_calls == [_URL]


def test_probe_failure_is_warning_and_other_hosts_continue(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={
            "bad": _remote_asset("https://down.example.com/x.parquet"),
            "good": _remote_asset("https://up.example.com/y.parquet"),
        },
    )
    graph = CatalogGraph.load(catalog.write())

    class FlakyProber(FakeProber):
        def get_range(self, url: str) -> ProbeResponse:
            if "down.example.com" in url:
                raise OSError("connection refused")
            return super().get_range(url)

    findings = validate_live(graph, FlakyProber())
    assert _ids(findings) == [LIV_UNAVAILABLE]
    assert findings[0].severity is Severity.WARNING
    assert "down.example.com" in findings[0].message


def test_head_failure_is_warning(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)

    class HeadlessProber(FakeProber):
        def head(self, url: str) -> ProbeResponse:
            raise OSError("timed out")

    findings = validate_live(graph, HeadlessProber())
    assert LIV_UNAVAILABLE in _ids(findings)


def test_non_https_and_relative_hrefs_are_skipped(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={
            "s3": _remote_asset("s3://bucket/x.parquet"),  # PTL-AST-002's domain
            "rel": _remote_asset("./data.parquet"),
        },
    )
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert prober.range_calls == []
    assert _ids(findings) == [LIV_UNAVAILABLE]  # nothing probeable


def test_malformed_objects_are_skipped(catalog: CatalogBuilder) -> None:
    catalog.collection("no-assets", assets="not-a-dict")
    catalog.collection("bad-asset", assets={"data": "not-a-dict"})
    catalog.collection("bad-href", assets={"data": {"href": 12345}})
    catalog.collection("broken")
    root = catalog.write()
    (root / "broken" / "collection.json").write_text("{ broken", encoding="utf-8")
    graph = CatalogGraph.load(root)
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert prober.range_calls == []  # none of it is probeable
    assert _ids(findings) == [LIV_UNAVAILABLE]


def test_runner_wires_live_pass(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _remote_asset()})
    root = catalog.write()
    prober = FakeProber(range_response=ProbeResponse(status=200, headers={}))
    report = validate(root, live=True, live_prober=prober)
    assert any(f.rule_id == LIV_RANGE for f in report.findings)


def test_live_off_by_default(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _remote_asset()})
    root = catalog.write()
    prober = FakeProber()
    validate(root, live_prober=prober)
    assert prober.range_calls == []


def test_disabling_all_liv_rules_skips_pass(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _remote_asset()})
    root = catalog.write()
    prober = FakeProber()
    config = RulesConfig(
        disabled=frozenset(
            {
                LIV_RANGE,
                LIV_HEAD_LENGTH,
                LIV_CORS_ORIGIN,
                LIV_CORS_EXPOSE,
                LIV_CORS_PREFLIGHT,
                LIV_LINK_TARGET,
                LIV_SELF_BASE,
                LIV_OFF_HTTPS,
            }
        )
    )
    report = validate(root, config=config, live=True, live_prober=prober)
    assert prober.range_calls == []
    assert all(not f.rule_id.startswith("PTL-LIV") for f in report.findings)


def test_disabling_one_liv_rule_silences_only_it(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": _remote_asset()})
    root = catalog.write()
    prober = FakeProber(
        range_response=ProbeResponse(status=200, headers={}),
        preflight_response=ProbeResponse(status=403, headers={}),
    )
    report = validate(
        root, config=RulesConfig(disabled=frozenset({LIV_RANGE})), live=True, live_prober=prober
    )
    ids = {f.rule_id for f in report.findings}
    assert LIV_RANGE not in ids
    assert LIV_CORS_PREFLIGHT in ids


# --- base_url: probing relative hrefs against the publish URL ----------------


def _graph_with_relative(catalog: CatalogBuilder) -> CatalogGraph:
    """The builder's default collection asset href is relative (./data.parquet)."""
    catalog.collection("roads")
    return CatalogGraph.load(catalog.write())


def test_base_url_makes_relative_hrefs_probeable(catalog: CatalogBuilder) -> None:
    graph = _graph_with_relative(catalog)
    prober = FakeProber()
    findings = validate_live(graph, prober, base_url="https://data.example.org/cat")
    assert LIV_UNAVAILABLE not in _ids(findings)
    # every relative asset (data + thumbnail) resolves under the publish base
    assert "https://data.example.org/cat/roads/data.parquet" in prober.head_calls
    assert all(u.startswith("https://data.example.org/cat/") for u in prober.head_calls)


def test_base_url_trailing_slash_is_normalized(catalog: CatalogBuilder) -> None:
    graph = _graph_with_relative(catalog)
    prober = FakeProber()
    validate_live(graph, prober, base_url="https://data.example.org/cat/")
    assert "https://data.example.org/cat/roads/data.parquet" in prober.head_calls
    assert not any("//cat//" in u or u.endswith("//") for u in prober.head_calls)


def test_base_url_probes_nested_items(catalog: CatalogBuilder) -> None:
    collection = catalog.collection("roads")
    collection.item("seg1")
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    validate_live(graph, prober, base_url="https://data.example.org/cat")
    assert any(
        url.startswith("https://data.example.org/cat/roads/seg1/") for url in prober.head_calls
    )


def test_base_url_skips_hrefs_escaping_the_tree(catalog: CatalogBuilder) -> None:
    asset = {
        "href": "../../outside.parquet",
        "type": "application/vnd.apache.parquet",
        "roles": ["data"],
    }
    catalog.collection("roads", assets={"data": asset})
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    findings = validate_live(graph, prober, base_url="https://data.example.org/cat")
    assert prober.range_calls == []  # no asset host to probe
    assert not any(u.endswith("outside.parquet") for u in prober.head_calls)
    assert LIV_UNAVAILABLE in _ids(findings)  # no asset probeable after the skip


def test_non_https_base_url_is_rejected(catalog: CatalogBuilder) -> None:
    graph = _graph_with_relative(catalog)
    with pytest.raises(ValueError, match="https"):
        validate_live(graph, FakeProber(), base_url="http://data.example.org/cat")


def test_without_base_url_relative_hrefs_stay_skipped(catalog: CatalogBuilder) -> None:
    """Regression guard: no base_url means byte-identical legacy behavior."""
    graph = _graph_with_relative(catalog)
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert prober.head_calls == []
    assert _ids(findings) == [LIV_UNAVAILABLE]


def test_relative_hrefs_probe_the_publish_host_only(catalog: CatalogBuilder) -> None:
    # data.example.com is a second host the catalog cites but does not publish
    # under, so the probe set is the publish host alone (PORTO-CORE-073).
    catalog.collection("roads")  # relative ./data.parquet
    catalog.collection("rails", assets={"data": _remote_asset()})  # absolute _URL
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    validate_live(graph, prober, base_url="https://data.example.org/cat")
    hosts = {u.split("/")[2] for u in prober.range_calls}
    assert hosts == {"data.example.org"}


def test_runner_threads_live_base_url(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    prober = FakeProber()
    validate(
        root,
        structural=False,
        live=True,
        live_prober=prober,
        live_base_url="https://data.example.org/cat",
    )
    assert "https://data.example.org/cat/roads/data.parquet" in prober.head_calls


# --- base_url: every link target must exist on the publish host (#189) --------
#
# The defect these guard: a publisher uploads the assets but not the nested
# catalog.json and item documents. Every asset probe passes; the tree's child
# and item links answer 404; a STAC browser hangs on the collection.

_BASE = "https://data.example.org/cat"


class StatusProber(FakeProber):
    """A fake whose HEAD answers per URL: 200 unless ``statuses`` says otherwise."""

    def __init__(self, statuses: dict[str, int] | None = None) -> None:
        super().__init__()
        self.statuses = statuses or {}

    def head(self, url: str) -> ProbeResponse:
        self.head_calls.append(url)
        status = self.statuses.get(url, 200)
        return ProbeResponse(status=status, headers={"content-length": "1234"})


def _nested_tree(catalog: CatalogBuilder) -> CatalogGraph:
    """root -> sector catalog -> roads collection -> two items, all relative links."""
    sector = catalog.subcatalog("sector-1")
    roads = sector.collection("roads")
    roads.item("seg1")
    roads.item("seg2")
    return CatalogGraph.load(catalog.write())


def _link_findings(findings: list) -> list:
    return [f for f in findings if f.rule_id == LIV_LINK_TARGET]


def _link_index(graph: CatalogGraph, path: str, rel: str, href: str) -> int:
    """The index of the ``rel`` link to ``href`` in the links array of ``path``."""
    node = graph.nodes[PurePosixPath(path)]
    for index, link in enumerate(node.data["links"]):
        if link["rel"] == rel and link["href"] == href:
            return index
    raise AssertionError(f"{path} has no {rel} link to {href}")


def test_complete_published_tree_has_no_link_findings(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)
    prober = StatusProber()
    findings = validate_live(graph, prober, base_url=_BASE)
    assert _link_findings(findings) == []
    # the documents behind child, item, parent, root, agents, describedby all got a HEAD
    for rel in (
        "catalog.json",
        "sector-1/catalog.json",
        "sector-1/roads/collection.json",
        "sector-1/roads/seg1/seg1.json",
        "sector-1/roads/seg2/seg2.json",
        "sector-1/roads/AGENTS.md",
        "sector-1/roads/README.md",
    ):
        assert f"{_BASE}/{rel}" in prober.head_calls


def test_missing_nested_catalog_is_one_error_on_the_child_link(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)
    missing = f"{_BASE}/sector-1/catalog.json"
    findings = validate_live(graph, StatusProber({missing: 404}), base_url=_BASE)
    (finding,) = _link_findings(findings)
    assert finding.severity is Severity.ERROR
    assert finding.path == "catalog.json"  # the document that links to the missing one
    assert finding.object_id == "root"
    index = _link_index(graph, "catalog.json", "child", "./sector-1/catalog.json")
    assert finding.json_pointer == f"/links/{index}/href"
    assert "rel:'child'" in finding.message
    assert "404" in finding.message
    assert missing in finding.message
    assert finding.actual == 404


def test_missing_item_document_is_an_error_on_the_item_link(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)
    missing = f"{_BASE}/sector-1/roads/seg2/seg2.json"
    findings = validate_live(graph, StatusProber({missing: 404}), base_url=_BASE)
    (finding,) = _link_findings(findings)
    assert finding.path == "sector-1/roads/collection.json"
    index = _link_index(graph, "sector-1/roads/collection.json", "item", "./seg2/seg2.json")
    assert finding.json_pointer == f"/links/{index}/href"
    assert "rel:'item'" in finding.message


def test_url_named_by_many_links_is_probed_once_and_reported_once(
    catalog: CatalogBuilder,
) -> None:
    # collection.json is the catalog's child, each item's parent, and each
    # item's collection: five links, one URL, one HEAD, one finding — against
    # the first link in path order, the catalog's child link.
    graph = _nested_tree(catalog)
    url = f"{_BASE}/sector-1/roads/collection.json"
    prober = StatusProber({url: 404})
    findings = validate_live(graph, prober, base_url=_BASE)
    assert prober.head_calls.count(url) == 1
    (finding,) = _link_findings(findings)
    assert finding.path == "sector-1/catalog.json"
    assert "rel:'child'" in finding.message


def test_root_link_from_every_object_is_probed_once(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)
    prober = StatusProber()
    validate_live(graph, prober, base_url=_BASE)
    assert prober.head_calls.count(f"{_BASE}/catalog.json") == 1


def test_link_on_another_host_is_not_probed(catalog: CatalogBuilder) -> None:
    # PORTO-CORE-073: the publish host is probed, upstream servers are not.
    catalog.collection(
        "roads",
        links=[
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "license", "href": "https://spdx.org/licenses/CC-BY-4.0.html"},
            {"rel": "via", "href": "https://upstream.example.gov/source.json"},
        ],
    )
    graph = CatalogGraph.load(catalog.write())
    prober = StatusProber({"https://spdx.org/licenses/CC-BY-4.0.html": 404})
    findings = validate_live(graph, prober, base_url=_BASE)
    assert all(u.startswith(f"{_BASE}/") for u in prober.head_calls)
    assert _link_findings(findings) == []


def test_absolute_link_on_the_publish_host_is_probed_as_declared(
    catalog: CatalogBuilder,
) -> None:
    root = CatalogBuilder(
        catalog.root,
        links=[
            {"rel": "self", "href": f"{_BASE}/catalog.json", "type": "application/json"},
            {"rel": "root", "href": "./catalog.json", "type": "application/json"},
        ],
    )
    graph = CatalogGraph.load(root.write())
    prober = StatusProber({f"{_BASE}/catalog.json": 404})
    findings = validate_live(graph, prober, base_url=_BASE)
    assert prober.head_calls == [f"{_BASE}/catalog.json"]
    (finding,) = _link_findings(findings)
    assert "rel:'self'" in finding.message  # the first link naming the URL


def test_without_base_url_no_link_is_probed(catalog: CatalogBuilder) -> None:
    """Regression guard: no base_url means the asset-only pass of before #189."""
    catalog.subcatalog("sector-1").collection("roads", assets={"data": _remote_asset()})
    graph = CatalogGraph.load(catalog.write())
    prober = StatusProber()
    findings = validate_live(graph, prober)
    assert prober.head_calls == [_URL]
    assert findings == []


def test_link_findings_follow_sorted_url_order(catalog: CatalogBuilder) -> None:
    """The HEADs after a host's first one run in parallel, in any order.

    Only the first request is sent alone, so it alone has a fixed place. The
    findings must not depend on which request finished first: they follow
    sorted URL order, and each URL is asked once.
    """
    graph = _nested_tree(catalog)
    asset_urls = {
        t.url for targets in live_mod._targets_by_host(graph, _BASE).values() for t in targets
    }
    base, _named = live_mod._resolve_base(graph, _BASE)
    link_urls = [t.url for t in live_mod._link_targets(graph, base)]
    assert link_urls == sorted(link_urls) and len(link_urls) > 2
    prober = StatusProber(dict.fromkeys(link_urls, 404))

    findings = validate_live(graph, prober, base_url=_BASE)

    link_calls = [u for u in prober.head_calls if u not in asset_urls]
    assert link_calls[0] == link_urls[0]
    assert sorted(link_calls) == link_urls
    reported = [re.search(r"HEAD (\S+) returned", f.message)[1] for f in _link_findings(findings)]
    assert reported == link_urls


def test_link_target_shared_with_an_asset_is_headed_once(catalog: CatalogBuilder) -> None:
    # A relative rel:'pmtiles' link names the same file as the pmtiles asset.
    pmtiles = {"href": "./tiles.pmtiles", "type": "application/vnd.pmtiles", "roles": ["data"]}
    catalog.collection(
        "roads",
        assets={"data": pmtiles},
        links=[
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "pmtiles", "href": "./tiles.pmtiles", "type": "application/vnd.pmtiles"},
        ],
    )
    graph = CatalogGraph.load(catalog.write())
    prober = StatusProber()
    validate_live(graph, prober, base_url=_BASE)
    assert prober.head_calls.count(f"{_BASE}/roads/tiles.pmtiles") == 1


def test_head_405_is_reported_not_retried_as_get(catalog: CatalogBuilder) -> None:
    # PORTO-CORE-043 makes HEAD a MUST for the publish host, so a 405 is the
    # finding, not a cue to fall back to GET.
    graph = _nested_tree(catalog)
    url = f"{_BASE}/sector-1/catalog.json"
    prober = StatusProber({url: 405})
    findings = validate_live(graph, prober, base_url=_BASE)
    (finding,) = _link_findings(findings)
    assert "405" in finding.message
    assert prober.range_calls == [f"{_BASE}/sector-1/roads/data.parquet"]  # no extra GET


def test_unreachable_publish_host_degrades_to_a_warning(catalog: CatalogBuilder) -> None:
    # A catalog with no assets: the link probes are the first to touch the host.
    graph = CatalogGraph.load(catalog.write())

    class DeadProber(StatusProber):
        def head(self, url: str) -> ProbeResponse:
            self.head_calls.append(url)
            raise OSError("connection refused")

    prober = DeadProber()
    findings = validate_live(graph, prober, base_url=_BASE)
    assert len(prober.head_calls) == 1  # not hammered after the first failure
    assert _ids(findings) == [LIV_UNAVAILABLE, LIV_UNAVAILABLE]  # no assets; host down
    assert "data.example.org" in findings[1].message
    assert all(f.severity is Severity.WARNING for f in findings)


def test_host_dead_for_assets_is_not_asked_about_links(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)

    class DeadProber(StatusProber):
        def get_range(self, url: str) -> ProbeResponse:
            raise OSError("connection refused")

    prober = DeadProber()
    findings = validate_live(graph, prober, base_url=_BASE)
    assert prober.head_calls == []
    assert _ids(findings) == [LIV_UNAVAILABLE]  # reported once, by the asset probes


def test_unparseable_document_contributes_no_link_probes(catalog: CatalogBuilder) -> None:
    catalog.subcatalog("broken")
    root = catalog.write()
    (root / "broken" / "catalog.json").write_text("{ broken", encoding="utf-8")
    graph = CatalogGraph.load(root)
    prober = StatusProber({f"{_BASE}/broken/catalog.json": 404})
    findings = validate_live(graph, prober, base_url=_BASE)
    # the root's child link still names the broken document, and that HEAD fails
    (finding,) = _link_findings(findings)
    assert finding.path == "catalog.json"
    assert not any(
        u.startswith(f"{_BASE}/broken/") and u != f"{_BASE}/broken/catalog.json"
        for u in prober.head_calls
    )


def test_runner_reports_missing_link_targets(catalog: CatalogBuilder) -> None:
    catalog.subcatalog("sector-1").collection("roads")
    root = catalog.write()
    prober = StatusProber({f"{_BASE}/sector-1/catalog.json": 404})
    report = validate(root, structural=False, live=True, live_prober=prober, live_base_url=_BASE)
    assert [f.rule_id for f in report.findings if f.rule_id == LIV_LINK_TARGET] == [LIV_LINK_TARGET]
    assert not report.passed


def test_disabling_link_target_rule_silences_it(catalog: CatalogBuilder) -> None:
    catalog.subcatalog("sector-1").collection("roads")
    root = catalog.write()
    prober = StatusProber({f"{_BASE}/sector-1/catalog.json": 404})
    report = validate(
        root,
        structural=False,
        config=RulesConfig(disabled=frozenset({LIV_LINK_TARGET})),
        live=True,
        live_prober=prober,
        live_base_url=_BASE,
    )
    assert LIV_LINK_TARGET not in {f.rule_id for f in report.findings}


# --- the root self link as the publish base (#160) ----------------------------
#
# core.md, Links: a published root catalog SHOULD carry an absolute self link
# (PORTO-CORE-081). It names the base every file is served under, so a catalog
# that carries one needs no --live-base-url; and when the caller names a base
# too, the two must agree.


def _self_linked_tree(catalog: CatalogBuilder, self_href: str = f"{_BASE}/catalog.json") -> Path:
    sector = catalog.subcatalog("sector-1")
    sector.collection("roads").item("seg1")
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append({"rel": "self", "href": self_href, "type": "application/json"}),
    )
    return root


def test_self_link_supplies_the_base_when_none_is_given(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog))
    prober = StatusProber()
    findings = validate_live(graph, prober)
    assert LIV_UNAVAILABLE not in _ids(findings)
    # relative assets and link targets both resolve under the self link's base
    assert f"{_BASE}/sector-1/roads/data.parquet" in prober.head_calls
    assert f"{_BASE}/sector-1/roads/seg1/seg1.json" in prober.head_calls
    assert all(u.startswith(f"{_BASE}/") for u in prober.head_calls)


def test_self_link_base_reports_missing_documents(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog))
    missing = f"{_BASE}/sector-1/catalog.json"
    findings = validate_live(graph, StatusProber({missing: 404}))
    [finding] = _link_findings(findings)
    assert finding.path == "catalog.json"
    assert missing in finding.message


def test_self_link_base_alone_raises_no_self_base_finding(catalog: CatalogBuilder) -> None:
    """With no caller base there is nothing to compare the self link against."""
    graph = CatalogGraph.load(_self_linked_tree(catalog))
    findings = validate_live(graph, StatusProber())
    assert LIV_SELF_BASE not in _ids(findings)


def test_caller_base_wins_over_the_self_link(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog))
    prober = StatusProber()
    validate_live(graph, prober, base_url="https://mirror.example.net/copy")
    assert all(u.startswith("https://mirror.example.net/copy/") for u in prober.head_calls)


def test_self_link_disagreeing_with_the_base_is_reported(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog))
    findings = validate_live(graph, StatusProber(), base_url="https://mirror.example.net/copy/")
    [finding] = [f for f in findings if f.rule_id == LIV_SELF_BASE]
    assert finding.severity is Severity.WARNING
    assert finding.path == "catalog.json"
    assert (
        finding.json_pointer
        == f"/links/{_link_index(graph, 'catalog.json', 'self', f'{_BASE}/catalog.json')}/href"
    )
    assert finding.expected == "https://mirror.example.net/copy/"
    assert finding.actual == f"{_BASE}/"
    assert "https://mirror.example.net/copy/catalog.json" in (finding.fix_hint or "")


def test_self_link_agreeing_with_the_base_is_silent(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog))
    for given in (_BASE, f"{_BASE}/"):
        findings = validate_live(graph, StatusProber(), base_url=given)
        assert LIV_SELF_BASE not in _ids(findings)


@pytest.mark.parametrize(
    "spelling",
    [
        "https://DATA.example.org/cat/catalog.json",
        "https://data.example.org:443/cat/catalog.json",
        "https://data.example.org/cat/./catalog.json",
        "HTTPS://data.example.org/cat/catalog.json",
    ],
)
def test_self_link_spellings_of_the_base_are_not_disagreements(
    catalog: CatalogBuilder, spelling: str
) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog, self_href=spelling))
    findings = validate_live(graph, StatusProber(), base_url=_BASE)
    assert LIV_SELF_BASE not in _ids(findings)


def test_self_base_pointer_names_the_absolute_self_link(catalog: CatalogBuilder) -> None:
    """A relative self link ahead of the absolute one must not steal the pointer."""
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].extend(
            [
                {"rel": "self", "href": "./catalog.json"},
                {"rel": "self", "href": "https://old.example.org/cat/catalog.json"},
            ]
        ),
    )
    graph = CatalogGraph.load(root)
    [finding] = [
        f
        for f in validate_live(graph, StatusProber(), base_url=_BASE)
        if f.rule_id == LIV_SELF_BASE
    ]
    index = _link_index(graph, "catalog.json", "self", "https://old.example.org/cat/catalog.json")
    assert finding.json_pointer == f"/links/{index}/href"


def test_self_link_without_a_host_is_ignored_not_fatal(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog, self_href="https://catalog.json"))
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert LIV_UNAVAILABLE in _ids(findings)
    assert prober.head_calls == []


def test_relative_asset_hrefs_are_percent_encoded_for_the_wire(catalog: CatalogBuilder) -> None:
    catalog.collection("roads", assets={"data": {"href": "./my file.parquet", "roles": ["data"]}})
    graph = CatalogGraph.load(catalog.write())
    prober = StatusProber()
    validate_live(graph, prober, base_url=_BASE)
    assert f"{_BASE}/roads/my%20file.parquet" in prober.head_calls


def test_relative_self_link_is_not_a_base(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(_self_linked_tree(catalog, self_href="./catalog.json"))
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert LIV_UNAVAILABLE in _ids(findings)
    assert prober.head_calls == []


def test_http_self_link_is_not_probed(catalog: CatalogBuilder) -> None:
    graph = CatalogGraph.load(
        _self_linked_tree(catalog, self_href="http://data.example.org/cat/catalog.json")
    )
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert LIV_UNAVAILABLE in _ids(findings)
    assert prober.head_calls == []


def test_no_base_hint_names_the_self_link(catalog: CatalogBuilder) -> None:
    graph = _graph_with_relative(catalog)
    [finding] = validate_live(graph, FakeProber())
    assert finding.rule_id == LIV_UNAVAILABLE
    assert "self link" in finding.message


def test_graph_base_url_is_used_when_no_base_is_passed(catalog: CatalogBuilder) -> None:
    """URL mode records the base on the graph; the pass picks it up."""
    graph = _nested_tree(catalog)
    graph.base_url = _BASE
    prober = StatusProber()
    findings = validate_live(graph, prober)
    assert LIV_UNAVAILABLE not in _ids(findings)
    assert f"{_BASE}/sector-1/roads/seg1/seg1.json" in prober.head_calls


def test_known_statuses_replace_link_heads(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)
    known = {f"{_BASE}/sector-1/catalog.json": 404, f"{_BASE}/sector-1/roads/collection.json": 200}
    prober = StatusProber()
    findings = validate_live(graph, prober, base_url=_BASE, known_statuses=known)
    [finding] = _link_findings(findings)
    assert f"{_BASE}/sector-1/catalog.json" in finding.message
    assert finding.actual == 404
    # neither known URL was asked again; the rest still were
    assert f"{_BASE}/sector-1/catalog.json" not in prober.head_calls
    assert f"{_BASE}/sector-1/roads/collection.json" not in prober.head_calls
    assert f"{_BASE}/sector-1/roads/seg1/seg1.json" in prober.head_calls


def test_known_status_does_not_stand_in_for_an_asset_head(catalog: CatalogBuilder) -> None:
    """A known status carries no Content-Length, so the asset check fetches its own."""
    graph = _graph_with_relative(catalog)
    asset = f"{_BASE}/roads/data.parquet"
    prober = StatusProber()
    findings = validate_live(graph, prober, base_url=_BASE, known_statuses={asset: 200})
    assert asset in prober.head_calls
    assert not any(f.rule_id == LIV_HEAD_LENGTH and "'data'" in f.message for f in findings)


def test_runner_reports_self_base_and_it_can_be_disabled(catalog: CatalogBuilder) -> None:
    root = _self_linked_tree(catalog)
    other = "https://mirror.example.net/copy"
    report = validate(root, live=True, live_prober=StatusProber(), live_base_url=other, data=False)
    [finding] = [f for f in report.findings if f.rule_id == LIV_SELF_BASE]
    assert finding.severity is Severity.WARNING  # a SHOULD: warns, does not fail the run
    report = validate(
        root,
        config=RulesConfig(disabled=frozenset({LIV_SELF_BASE})),
        live=True,
        live_prober=StatusProber(),
        live_base_url=other,
        data=False,
    )
    assert LIV_SELF_BASE not in {f.rule_id for f in report.findings}


# --- the default prober's outgoing request headers ---------------------------
#
# The fake prober above never builds a Request, so a missing or malformed
# User-Agent is invisible to every test that uses it. These drive the real
# _DefaultProber against a recording opener instead (#64). Its HEAD goes to
# obstore first, so _record makes obstore fail and the HEAD falls back.


class _RecordedResponse:
    """The bare surface ``_request`` reads off an opened response."""

    status = 200
    headers: dict[str, str] = {}

    def __enter__(self) -> _RecordedResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _recorder(requests: list[Request]) -> Any:
    def fake_open(request: Request) -> _RecordedResponse:
        requests.append(request)
        return _RecordedResponse()

    return fake_open


def _store_fails(url: str) -> int:
    raise _store.StoreError(f"HEAD {url} returned 404", 404)


def _record(monkeypatch: pytest.MonkeyPatch, probe: str) -> Request:
    """Run one probe kind against a recording opener and return its Request."""
    requests: list[Request] = []
    monkeypatch.setattr(live_mod, "_open", _recorder(requests))
    monkeypatch.setattr(live_mod._store, "head_size", _store_fails)
    getattr(_DefaultProber(), probe)(_URL)
    (request,) = requests
    return request


# urllib capitalizes header names, so the stored key is 'User-agent'.
_AGENT_PATTERN = re.compile(r"^rashid/\S+ \(\+https://github\.com/portolan-sdi/rashid\)$")


@pytest.mark.parametrize("probe", ["get_range", "head", "preflight"])
def test_every_probe_sends_a_named_user_agent(monkeypatch: pytest.MonkeyPatch, probe: str) -> None:
    agent = _record(monkeypatch, probe).get_header("User-agent")
    assert agent is not None, f"{probe} sent no User-Agent"
    assert _AGENT_PATTERN.match(agent), agent
    assert "Python-urllib" not in agent


@pytest.mark.parametrize("probe", ["get_range", "head", "preflight"])
def test_user_agent_carries_the_installed_version(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    agent = _record(monkeypatch, probe).get_header("User-agent")
    assert agent == f"rashid/{version('rashid')} (+https://github.com/portolan-sdi/rashid)"


def test_user_agent_does_not_displace_the_probe_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    ranged = _record(monkeypatch, "get_range")
    assert ranged.get_header("Range") == "bytes=0-0"
    assert ranged.get_header("Origin") is not None

    preflight = _record(monkeypatch, "preflight")
    assert preflight.get_header("Access-control-request-method") == "GET"
    assert preflight.get_header("Access-control-request-headers") == (
        "Range, If-Match, If-Modified-Since, If-None-Match, If-Unmodified-Since"
    )


# --- the default prober's HEAD: obstore first, urllib for what it drops -----


def test_default_head_reads_the_size_through_obstore(https_server: HttpsServer) -> None:
    https_server.routes["/x.parquet"] = Route(body=b"abcdef")

    response = _DefaultProber().head(https_server.url("x.parquet"))

    assert response == ProbeResponse(status=200, headers={"content-length": "6"})
    (seen,) = https_server.seen
    assert seen.method == "HEAD"
    assert _AGENT_PATTERN.match(seen.headers["user-agent"])


@pytest.mark.parametrize(
    "failure",
    [
        _store.StoreError("HEAD returned 404", 404),
        _store.StoreError("HEAD returned no Content-Length", refused=True),
        _store.StoreError("redirect to a non-https URL refused", refused=True),
    ],
    ids=["status", "no-content-length", "redirect-off-https"],
)
def test_default_head_falls_back_to_urllib(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """An answer obstore drops is asked again over urllib, which keeps the headers."""

    def fail(url: str) -> int:
        raise failure

    requests: list[Request] = []
    monkeypatch.setattr(live_mod._store, "head_size", fail)
    monkeypatch.setattr(live_mod, "_open", _recorder(requests))

    response = _DefaultProber().head(_URL)

    assert response.status == 200
    (request,) = requests
    assert request.get_method() == "HEAD"


def test_default_head_does_not_ask_a_dead_host_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport failure has no answer to read again; urllib would only wait again."""
    dead = _store.StoreError("HEAD https://data.example.org/x: error sending request")

    def fail(url: str) -> int:
        raise dead

    requests: list[Request] = []
    monkeypatch.setattr(live_mod._store, "head_size", fail)
    monkeypatch.setattr(live_mod, "_open", _recorder(requests))

    with pytest.raises(_store.StoreError) as raised:
        _DefaultProber().head(_URL)
    assert raised.value is dead
    assert requests == []


def test_default_head_reports_a_dead_host_once(catalog: CatalogBuilder) -> None:
    """The real prober against a closed port: one PTL-LIV-000, no urllib retry."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    catalog.collection(
        "roads",
        assets={f"a{n}": _remote_asset(f"https://127.0.0.1:{port}/{n}.parquet") for n in range(3)},
    )
    graph = CatalogGraph.load(catalog.write())

    class HeadsOnly(_DefaultProber):
        def get_range(self, url: str) -> ProbeResponse:
            return _good_head("1")

        def preflight(self, url: str) -> ProbeResponse:
            return _good_head("1")

    findings = validate_live(graph, HeadsOnly())

    unavailable = [f for f in findings if f.rule_id == LIV_UNAVAILABLE]
    assert len(unavailable) == 1
    assert "error sending request" in unavailable[0].message


def test_parallel_heads_report_a_failure_in_order(catalog: CatalogBuilder) -> None:
    """A failure mid-batch is the host's failure only when the checks reach it."""
    catalog.collection(
        "roads",
        assets={f"a{n}": _remote_asset(f"https://data.example.org/{n}.parquet") for n in range(6)},
    )
    graph = CatalogGraph.load(catalog.write())
    broken = "https://data.example.org/3.parquet"

    class BrokenProber(FakeProber):
        def head(self, url: str) -> ProbeResponse:
            self.head_calls.append(url)
            if url == broken:
                raise OSError("reset by peer")
            return _good_head("999")  # every earlier asset mismatches file:size

    findings = validate_live(graph, BrokenProber())

    assert _ids(findings) == [LIV_HEAD_LENGTH] * 3 + [LIV_UNAVAILABLE]
    assert [f.json_pointer for f in findings[:3]] == ["/assets/a0", "/assets/a1", "/assets/a2"]
    assert "reset by peer" in findings[3].message


def test_the_first_head_on_a_host_goes_alone(catalog: CatalogBuilder) -> None:
    catalog.collection(
        "roads",
        assets={f"a{n}": _remote_asset(f"https://data.example.org/{n}.parquet") for n in range(6)},
    )
    graph = CatalogGraph.load(catalog.write())

    class DeadProber(FakeProber):
        def head(self, url: str) -> ProbeResponse:
            self.head_calls.append(url)
            raise OSError("connection refused")

    prober = DeadProber()
    findings = validate_live(graph, prober)

    assert prober.head_calls == ["https://data.example.org/0.parquet"]
    assert _ids(findings) == [LIV_UNAVAILABLE]


# --- a redirect off https is reported, never followed (#226) ----------------
#
# The defect these guard: urllib's default opener follows a redirect from
# https to plain http. The probe then sent a plain-text request to the host the
# redirect named, and the live pass reported that answer as the asset's.


class _PlainHttp:
    """A plain-http server that records the path of every request it gets."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return None

            def _answer(self) -> None:
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Content-Length", "1")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(b"x")

            do_GET = do_HEAD = do_OPTIONS = _answer

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.02,), daemon=True)

    def __enter__(self) -> _PlainHttp:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def plain_http() -> Iterator[_PlainHttp]:
    with _PlainHttp() as server:
        yield server


def _off_https(https_server: HttpsServer, plain_http: _PlainHttp, path: str) -> str:
    """Route ``path`` on the https server to a 302 that points at the http server."""
    target = f"{plain_http.url}/moved.bin"
    https_server.routes[path] = Route(status=302, headers={"Location": target})
    return target


@pytest.mark.parametrize("probe", ["get_range", "head", "preflight"])
def test_a_probe_answers_a_redirect_off_https_and_sends_nothing_to_http(
    https_server: HttpsServer, plain_http: _PlainHttp, probe: str
) -> None:
    target = _off_https(https_server, plain_http, "/x.bin")

    response = getattr(_DefaultProber(), probe)(https_server.url("x.bin"))

    assert response.status == 302
    assert response.off_https() == target
    assert plain_http.hits == []


def test_a_redirect_that_stays_on_https_is_followed_with_its_headers(
    https_server: HttpsServer,
) -> None:
    https_server.routes["/old.bin"] = Route(status=301, headers={"Location": "/new.bin"})
    https_server.routes["/new.bin"] = Route(body=b"abc", headers={"Accept-Ranges": "bytes"})

    response = _DefaultProber().get_range(https_server.url("old.bin"))

    assert response.status == 206
    assert response.header("accept-ranges") == "bytes"
    assert response.off_https() is None
    assert [s.path for s in https_server.seen] == ["/old.bin", "/new.bin"]


def test_live_pass_reports_an_asset_that_redirects_off_https(
    catalog: CatalogBuilder, https_server: HttpsServer, plain_http: _PlainHttp
) -> None:
    """The real prober end to end: one PTL-LIV-008 per asset, no plain-http request."""
    target = _off_https(https_server, plain_http, "/a.bin")
    _off_https(https_server, plain_http, "/b.bin")
    https_server.routes["/c.bin"] = Route(body=b"x" * 1234)
    catalog.collection(
        "roads",
        assets={
            key: _remote_asset(https_server.url(f"{key}.bin"), size=1234) for key in ("a", "b", "c")
        },
    )

    findings = validate_live(CatalogGraph.load(catalog.write()), _DefaultProber())

    redirects = [f for f in findings if f.rule_id == LIV_OFF_HTTPS]
    assert [f.json_pointer for f in redirects] == ["/assets/a/href", "/assets/b/href"]
    assert all(f.severity is Severity.ERROR for f in redirects)
    assert redirects[0].actual == target
    assert target in redirects[0].message
    # Asset c answers its own HEAD with the declared size, so it passes.
    assert LIV_HEAD_LENGTH not in _ids(findings)
    assert plain_http.hits == []


def test_a_ranged_get_off_https_skips_the_range_and_cors_checks(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    moved = ProbeResponse(status=302, headers={"location": "http://data.example.com/moved"})
    prober = FakeProber(range_response=moved, head_response=moved, preflight_response=moved)

    findings = validate_live(graph, prober)

    # The ranged GET and the HEAD of the one asset redirect: one finding, not
    # two. No other asset can stand in, so a warning says the host checks did
    # not run.
    assert _ids(findings) == [LIV_OFF_HTTPS, LIV_UNAVAILABLE]
    assert "a ranged GET of" in findings[0].message
    assert "did not run: every asset on it redirects off https" in findings[1].message


def test_a_head_off_https_is_reported_instead_of_its_length(catalog: CatalogBuilder) -> None:
    graph = _graph_with_remote(catalog)
    moved = ProbeResponse(
        status=301, headers={"location": "http://data.example.com/moved", "content-length": "0"}
    )

    findings = validate_live(graph, FakeProber(head_response=moved))

    assert _ids(findings) == [LIV_OFF_HTTPS]
    assert findings[0].json_pointer == "/assets/data/href"
    assert "HEAD" in findings[0].message


def test_a_link_target_off_https_is_reported_as_the_redirect(catalog: CatalogBuilder) -> None:
    graph = _nested_tree(catalog)
    moved = f"{_BASE}/sector-1/catalog.json"

    class RedirectingProber(StatusProber):
        def head(self, url: str) -> ProbeResponse:
            if url == moved:
                self.head_calls.append(url)
                return ProbeResponse(
                    status=302, headers={"location": "http://data.example.org/c.json"}
                )
            return super().head(url)

    findings = validate_live(graph, RedirectingProber(), base_url=_BASE)

    assert _link_findings(findings) == []
    [finding] = [f for f in findings if f.rule_id == LIV_OFF_HTTPS]
    index = _link_index(graph, "catalog.json", "child", "./sector-1/catalog.json")
    assert finding.json_pointer == f"/links/{index}/href"
    assert finding.actual == "http://data.example.org/c.json"


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (302, {"location": "https://elsewhere.example.org/x"}),
        (302, {"location": "/relative/x"}),
        (304, {}),
        (200, {"location": "http://data.example.org/x"}),
    ],
    ids=["https-location", "relative-location", "no-location", "not-a-redirect"],
)
def test_only_a_redirect_to_another_scheme_is_off_https(
    status: int, headers: dict[str, str]
) -> None:
    assert ProbeResponse(status=status, headers=headers).off_https() is None


_MOVED = ProbeResponse(status=302, headers={"location": "http://data.example.com/moved"})


class _PerUrlProber(FakeProber):
    """FakeProber whose answers for the URLs in ``moved`` redirect off https."""

    def __init__(self, moved: set[str], range_moved: set[str] = frozenset(), **kw: Any) -> None:
        super().__init__(**kw)
        self.moved = moved
        self.range_moved = moved | range_moved

    def get_range(self, url: str) -> ProbeResponse:
        self.range_calls.append(url)
        return _MOVED if url in self.range_moved else self.range_response

    def head(self, url: str) -> ProbeResponse:
        self.head_calls.append(url)
        return _MOVED if url in self.moved else self.head_response

    def preflight(self, url: str) -> ProbeResponse:
        self.preflight_calls.append(url)
        return _MOVED if url in self.range_moved else self.preflight_response


def _two_assets(catalog: CatalogBuilder) -> tuple[CatalogGraph, str, str]:
    first, second = "https://data.example.com/a.parquet", "https://data.example.com/b.parquet"
    catalog.collection("roads", assets={"a": _remote_asset(first), "b": _remote_asset(second)})
    return CatalogGraph.load(catalog.write()), first, second


def test_the_next_asset_stands_in_when_the_first_redirects_off_https(
    catalog: CatalogBuilder,
) -> None:
    graph, first, second = _two_assets(catalog)
    no_cors = _good_range()
    del no_cors.headers["access-control-allow-origin"]
    prober = _PerUrlProber({first}, range_response=no_cors)

    findings = validate_live(graph, prober)

    assert prober.range_calls == [first, second]
    assert prober.preflight_calls == [first, second]
    assert _ids(findings) == [LIV_OFF_HTTPS, LIV_CORS_ORIGIN]
    assert findings[0].json_pointer == "/assets/a/href"
    assert findings[1].json_pointer == "/assets/b/href"


def test_every_asset_off_https_warns_that_the_host_checks_did_not_run(
    catalog: CatalogBuilder,
) -> None:
    graph, first, second = _two_assets(catalog)
    prober = _PerUrlProber({first, second})

    findings = validate_live(graph, prober)

    # No stand-in qualifies, so no second ranged GET goes out.
    assert prober.range_calls == [first]
    assert _ids(findings) == [LIV_OFF_HTTPS, LIV_UNAVAILABLE, LIV_OFF_HTTPS]
    assert [f.json_pointer for f in findings if f.rule_id == LIV_OFF_HTTPS] == [
        "/assets/a/href",
        "/assets/b/href",
    ]


def test_a_stand_in_whose_ranged_get_redirects_too_is_reported_and_warned(
    catalog: CatalogBuilder,
) -> None:
    graph, first, second = _two_assets(catalog)
    # b answers its HEAD in place, but its ranged GET redirects off https.
    prober = _PerUrlProber({first}, range_moved={second})

    findings = validate_live(graph, prober)

    assert prober.range_calls == [first, second]
    assert _ids(findings) == [LIV_OFF_HTTPS, LIV_OFF_HTTPS, LIV_UNAVAILABLE]
    assert "every asset tried" in findings[2].message
