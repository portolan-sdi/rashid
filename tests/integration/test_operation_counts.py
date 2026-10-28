"""Exact operation counts, and a memory ceiling, over the seeded scenarios.

A performance change that claims to do less work changes a number here. The
change updates the number in the same pull request, so the claim is checked
by CI rather than by a hand-run timer. A change that makes rashid do more work
without saying so fails here.

Each run is measured after one warm-up run over the same tree. The first run
in a process also parses the bundled schemas into ``lru_cache`` registries,
and that one-time cost would make the counts depend on test order.
"""

from __future__ import annotations

import tracemalloc
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

import rashid
from rashid import _trace
from rashid.model import Finding
from tests import scenarios
from tests._https import DOCUMENT_SUFFIXES, Seen, ServedTree

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("scenarios")


def _steady_counts(name: str, size: str, cache_dir: Path) -> dict[str, int]:
    with scenarios.target(name, size, cache_dir=cache_dir) as target:
        rashid.validate(target.location, **target.options)
        with _trace.counting() as delta:
            rashid.validate(target.location, **target.options)
    return delta


def test_metadata_small_counts(cache_dir: Path) -> None:
    # 1 root + 2 collections + 100 items = 103 STAC documents.
    assert _steady_counts("metadata", "small", cache_dir) == {
        "json.parses": 103,
        "rules.checks": 1821,
        "schema.rust_compiles": 3,
        "schema.rust_passes": 103,
    }


def test_metadata_large_counts(cache_dir: Path) -> None:
    # 1 root + 20 collections + 2000 items = 2021. The schema compiles stay
    # at 3: they depend on the schemas in use, not on the object count.
    assert _steady_counts("metadata", "large", cache_dir) == {
        "json.parses": 2021,
        "rules.checks": 35003,
        "schema.rust_compiles": 3,
        "schema.rust_passes": 2021,
    }


def test_data_small_counts(cache_dir: Path) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    counts = _steady_counts("data", "small", cache_dir)
    # 2 GeoParquet items + 2 COG items + the collection's thumbnail.
    assert counts["data.assets"] == 5
    # The checks on one asset share one open (#224). Before, each GeoParquet
    # asset opened its footer 3 times (the GeoParquet, vector-column, and
    # consistency checks), and each COG opened 4 times plus one cog_validate.
    assert counts["parquet.opens"] == 2
    assert counts["raster.opens"] == 2
    assert counts["raster.cog_validates"] == 2
    root = scenarios.cached("data", "small", cache_dir=cache_dir)
    on_disk = sum(p.stat().st_size for p in root.rglob("data.*"))
    assert counts["data.bytes_read"] == on_disk
    assert "http.gets" not in counts


def test_data_large_counts(cache_dir: Path) -> None:
    """One open per asset holds at 20 assets of each kind, not only at 2."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    counts = _steady_counts("data", "large", cache_dir)
    assert counts["data.assets"] == 20 + 20 + 1
    assert counts["parquet.opens"] == 20
    assert counts["raster.opens"] == 20
    assert counts["raster.cog_validates"] == 20


def _remote_data_run(
    cache_dir: Path, prepare: Callable[[ServedTree], None] | None = None
) -> tuple[list[Seen], int, list[Finding]]:
    """One run of ``remote-data/small``, on a fresh server.

    GDAL keeps a process-wide cache of what it read from each URL, so a second
    run against the same server would see fewer requests than the first. Each
    call serves the tree on a new port, which gives the run new URLs.
    ``prepare`` sets up the server before the run.
    """
    with scenarios.target("remote-data", "small", cache_dir=cache_dir) as target:
        if prepare is not None:
            prepare(target.server)
        report = rashid.validate(target.location, **target.options)
        return target.server.seen(), target.server.peak_in_flight(), report.findings


def test_remote_data_requests_per_asset(cache_dir: Path) -> None:
    """Each remote asset costs a fixed number of requests, whatever GDAL defaults to.

    Without ``EMPTY_DIR``, every raster open also lists the asset's directory
    and sends a HEAD for each of 7 sidecar names. Those requests leave from
    rasterio calls that hold the GIL, so on a published host they stall every
    thread in the pool too (#219). Before this test existed, on GDAL 3.12, a
    COG cost 44 requests here and a GeoParquet 7: 103 for the scenario, now 15.
    """
    pytest.importorskip("pyarrow")
    rasterio = pytest.importorskip("rasterio")
    seen, _peak, _findings = _remote_data_run(cache_dir)
    # The crawl's documents and the AGENTS.md and README.md beside them are
    # not assets.
    per_request = Counter(
        (s.method, s.path) for s in seen if not s.path.endswith(DOCUMENT_SUFFIXES)
    )
    expected: Counter[tuple[str, str]] = Counter({("GET", "/layers/thumbnail.png"): 1})
    for i in range(2):
        for name in (f"points-{i:03d}/data.parquet", f"raster-{i:03d}/data.tif"):
            # One HEAD for the size, one GET streaming the whole object for
            # the checksum, and one range GET for the footer or the header.
            expected[("HEAD", f"/layers/{name}")] = 1
            expected[("GET", f"/layers/{name}")] = 2
        # GDAL 3.11 reads an ArcGIS raster attribute table from a .vat.dbf
        # sidecar, and offers no option to skip the lookup.
        if tuple(int(n) for n in rasterio.__gdal_version__.split(".")[:2]) >= (3, 11):
            expected[("HEAD", f"/layers/raster-{i:03d}/data.tif.vat.dbf")] = 1
    assert per_request == expected


def test_remote_data_reads_in_parallel(cache_dir: Path) -> None:
    """The data pass holds several asset requests open at once.

    The server counts asset requests only, not the crawl's, which fetches its
    documents on many threads. Each object reads its own assets one at a
    time, so a serial loop over objects holds at most one. This fails on a
    return to it, with no wall-clock threshold to flake on a slow runner.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    _seen, peak, _findings = _remote_data_run(cache_dir)
    assert peak >= 2


def _data_findings(findings: list[Finding]) -> list[tuple[object, ...]]:
    return [
        (f.rule_id, f.severity, f.path, f.json_pointer, f.message)
        for f in findings
        if f.rule_id.startswith("PTL-DAT-")
    ]


def test_remote_data_raster_open_retries_a_throttled_request(cache_dir: Path) -> None:
    """GDAL retries a 503, so a throttling host does not make a valid COG broken.

    GDAL sends no retry by default. The first two of GDAL's requests for
    one COG answer 503 here, and the findings must still match a local run.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    tif = "/layers/raster-000/data.tif"
    seen, _peak, remote = _remote_data_run(cache_dir, lambda server: server.fail(tif, 2, "GDAL"))
    local = rashid.validate(scenarios.cached("data", "small", cache_dir=cache_dir)).findings

    assert _data_findings(remote) == _data_findings(local)
    gdal = [s for s in seen if s.path == tif and "GDAL" in s.headers.get("user-agent", "")]
    assert len(gdal) >= 3  # two refused, then the one that succeeded


def test_remote_data_findings_match_local(cache_dir: Path) -> None:
    """Serving the assets over HTTPS changes no data-pass finding."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    _seen, _peak, remote = _remote_data_run(cache_dir)
    local = rashid.validate(scenarios.cached("data", "small", cache_dir=cache_dir)).findings

    assert _data_findings(remote) == _data_findings(local)
    assert _data_findings(local)  # the scenario reports something to compare


def test_remote_small_counts(cache_dir: Path) -> None:
    counts = _steady_counts("remote", "small", cache_dir)
    assert counts["crawl.documents"] == 103
    # The crawl also GETs the 6 AGENTS.md and README.md files it follows.
    assert counts["http.gets"] == 103 + 6
    # One HEAD per asset: 2 per collection and 1 per item. The link check
    # reads the crawl's statuses and sends no HEAD of its own.
    assert counts["live.heads"] == counts["http.heads"] == 2 * 2 + 100
    assert counts["live.known_statuses"] == 109
    assert counts["live.range_probes"] == counts["live.preflights"] == 1
    assert "http.retries" not in counts
    assert "http.urllib_fallbacks" not in counts


def test_metadata_large_traced_memory_ceiling(cache_dir: Path) -> None:
    """The metadata pass over 2021 objects stays under a fixed traced peak.

    tracemalloc sees Python allocations only. jsonschema-rs, Arrow, and GDAL
    allocate outside it, so this ceiling covers the metadata pass, which
    holds every parsed object in ``CatalogGraph.nodes``. Measured in
    October 2026 after a warm-up run: 11.3 MiB for large, 0.6 MiB for small,
    and 5.9 KiB more for each extra object. Each limit leaves about 40%
    headroom. The limit per object guards the slope, which a fixed ceiling
    hides on a small input.
    """
    peaks: dict[str, int] = {}
    for size in scenarios.SIZES:
        root = scenarios.cached("metadata", size, cache_dir=cache_dir)
        rashid.validate(root, data=False)
        tracemalloc.start()
        try:
            rashid.validate(root, data=False)
            peaks[size] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert peaks["large"] < 16 * 2**20
    per_object = (peaks["large"] - peaks["small"]) / (2021 - 103)
    assert per_object < 8 * 1024


def test_timings_cover_every_stage_of_a_url_run(cache_dir: Path) -> None:
    _trace.enable("on")
    with scenarios.target("remote", "small", cache_dir=cache_dir) as target:
        rashid.validate(target.location, **target.options)
    names = [node["name"] for node in _trace.timings()]
    assert names == ["crawl", "load", "rules", "structural", "extensions", "live"]
    crawl = _trace.timings()[0]
    # The crawl's GETs ran on pool threads and still nest under the crawl.
    assert [child["name"] for child in crawl["children"]] == ["http.get"]
    assert crawl["children"][0]["calls"] == 109
