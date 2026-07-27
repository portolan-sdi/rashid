"""Live-hosting pass tests.

The default prober reaches the network, so every deterministic test injects a
fake ``Prober``; the real-HTTP path is covered by the ``network``-marked
integration test in ``tests/integration/test_live_remote.py``.
"""

from __future__ import annotations

import pytest

from rashid import RulesConfig, validate, validate_live
from rashid.catalog import CatalogGraph
from rashid.live import (
    LIV_CORS_EXPOSE,
    LIV_CORS_ORIGIN,
    LIV_CORS_PREFLIGHT,
    LIV_HEAD_LENGTH,
    LIV_RANGE,
    LIV_UNAVAILABLE,
    ProbeResponse,
)
from rashid.model import Severity
from tests.conftest import VALID_MULTIHASH, CatalogBuilder

pytestmark = pytest.mark.unit

_URL = "https://data.example.com/roads.parquet"


def _good_range(**overrides: str) -> ProbeResponse:
    headers = {
        "accept-ranges": "bytes",
        "content-length": "1",
        "access-control-allow-origin": "*",
        "access-control-expose-headers": "Content-Range, Content-Length, Accept-Ranges, ETag",
    }
    headers.update(overrides)
    return ProbeResponse(status=206, headers=headers)


def _good_head(length: str = "1234") -> ProbeResponse:
    return ProbeResponse(status=200, headers={"content-length": length})


def _good_preflight(**overrides: str) -> ProbeResponse:
    headers = {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, HEAD",
        "access-control-allow-headers": "Range",
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
        **{"access-control-expose-headers": "Content-Range, Content-Length, Accept-Ranges"}
    )
    findings = validate_live(graph, FakeProber(range_response=response))
    assert LIV_CORS_EXPOSE in _ids(findings)
    message = next(f.message for f in findings if f.rule_id == LIV_CORS_EXPOSE)
    assert "ETag" in message
    assert "Content-Range" not in message  # present ones are not reported


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


def test_source_and_alternate_assets_are_not_probed(catalog: CatalogBuilder) -> None:
    # A source/alternate original lives on a server the publisher does not
    # control (census.gov, an agency API); the hosting MUSTs bind the servers
    # hosting the cloud-native primaries. Mirrors the data pass exemption.
    source = _remote_asset("https://www2.census.gov/geo/tiger/counties.zip")
    source["roles"] = ["data", "source"]
    alternate = _remote_asset("https://api.example.gov/export.geojson")
    alternate["roles"] = ["alternate"]
    catalog.collection("roads", assets={"source": source, "alternate": alternate})
    graph = CatalogGraph.load(catalog.write())
    prober = FakeProber()
    findings = validate_live(graph, prober)
    assert prober.range_calls == []
    assert prober.head_calls == []
    assert _ids(findings) == [LIV_UNAVAILABLE]  # nothing probeable remains


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
            {LIV_RANGE, LIV_HEAD_LENGTH, LIV_CORS_ORIGIN, LIV_CORS_EXPOSE, LIV_CORS_PREFLIGHT}
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
