"""URL mode: reading a published catalog by following its links (#160).

The default fetcher reaches the network, so every test here serves a
``CatalogBuilder`` tree through a fake that answers from disk. That keeps the
documents real — the same links, sidecars, and assets a directory walk would
see — while letting a test remove a document from the host without removing
it from the tree, which is the fault URL mode exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
from urllib.request import Request

import pytest
from click.testing import CliRunner

from rashid import validate
from rashid.catalog import CatalogGraph
from rashid.cli import main
from rashid.data.reader import FilesystemHttpReader, LocalOnlyReader
from rashid.live import LIV_LINK_TARGET, LIV_SELF_BASE, LIV_UNAVAILABLE
from rashid.model import Severity
from rashid.remote import (
    Crawl,
    Fetched,
    FetchError,
    _UrllibFetcher,
    crawl,
    is_catalog_url,
    split_catalog_url,
)
from rashid.runner import GEN_MISSING_ROOT, GEN_PARTIAL_TREE
from tests.conftest import CatalogBuilder, mutate_json
from tests.unit.test_live import FakeProber, StatusProber

pytestmark = pytest.mark.unit

_BASE = "https://data.example.org/cat/"
_ROOT_URL = f"{_BASE}catalog.json"


class DirFetcher:
    """Serve a directory as if published under ``base``.

    ``missing`` names paths under the base that answer 404 although the file
    exists on disk — the publisher skipped them. ``broken`` names paths whose
    fetch raises, as a dead host does.
    """

    def __init__(
        self,
        root: Path,
        base: str = _BASE,
        missing: set[str] = frozenset(),  # type: ignore[assignment]
        broken: set[str] = frozenset(),  # type: ignore[assignment]
    ) -> None:
        self.root = root
        self.base = base
        self.missing = set(missing)
        self.broken = set(broken)
        self.calls: list[str] = []

    def get(self, url: str) -> Fetched:
        self.calls.append(url)
        assert url.startswith(self.base), url
        rel = unquote(url[len(self.base) :])
        if rel in self.broken:
            raise FetchError(f"GET {url}: connection refused")
        if rel in self.missing:
            return Fetched(status=404)
        path = self.root.joinpath(*rel.split("/"))
        if not path.is_file():
            return Fetched(status=404)
        return Fetched(status=200, body=path.read_bytes())


def _nested(catalog: CatalogBuilder) -> Path:
    """root -> sector catalog -> roads collection -> two items, all relative links."""
    sector = catalog.subcatalog("sector-1")
    roads = sector.collection("roads")
    roads.item("seg1")
    roads.item("seg2")
    return catalog.write()


def _rel_files(dest: Path) -> set[str]:
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}


# --- URL parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "location, expected",
    [
        ("https://h/cat", True),
        ("https://h/cat/catalog.json", True),
        ("http://h/cat", False),
        ("s3://bucket/cat", False),
        ("cat", False),
        ("/tmp/cat", False),
        ("https:///no-host", False),
    ],
)
def test_is_catalog_url(location: str, expected: bool) -> None:
    assert is_catalog_url(location) is expected


@pytest.mark.parametrize(
    "url",
    [
        "https://data.example.org/cat",
        "https://data.example.org/cat/",
        "https://data.example.org/cat/catalog.json",
        "https://data.example.org/cat/catalog.json?x=1#frag",
    ],
)
def test_split_catalog_url_names_the_same_catalog_either_way(url: str) -> None:
    assert split_catalog_url(url) == (_BASE, _ROOT_URL)


def test_split_catalog_url_at_the_host_root() -> None:
    assert split_catalog_url("https://h") == ("https://h/", "https://h/catalog.json")
    assert split_catalog_url("https://h/catalog.json") == ("https://h/", "https://h/catalog.json")


def test_split_catalog_url_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="https"):
        split_catalog_url("http://h/cat")


# --- the crawl -----------------------------------------------------------------


def test_crawl_writes_every_linked_document_and_sidecar(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = _nested(catalog)
    dest = tmp_path / "dest"
    result = crawl(_BASE, dest, DirFetcher(root))
    assert result.documents == 5  # root, sector, roads, seg1, seg2
    assert not result.capped
    assert result.errors == {}
    assert _rel_files(dest) == {
        "catalog.json",
        "AGENTS.md",
        "README.md",
        "sector-1/catalog.json",
        "sector-1/AGENTS.md",
        "sector-1/README.md",
        "sector-1/roads/collection.json",
        "sector-1/roads/AGENTS.md",
        "sector-1/roads/README.md",
        "sector-1/roads/seg1/seg1.json",
        "sector-1/roads/seg2/seg2.json",
    }
    # the crawled tree is byte-identical to the published one
    for rel in _rel_files(dest):
        assert (dest / rel).read_bytes() == (root / rel).read_bytes()


def test_crawl_does_not_fetch_assets(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    fetcher = DirFetcher(root)
    crawl(_BASE, tmp_path / "dest", fetcher)
    assert not any(url.endswith((".parquet", ".png", ".tif")) for url in fetcher.calls)


def test_crawl_records_a_status_for_every_url_asked(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = _nested(catalog)
    fetcher = DirFetcher(root, missing={"sector-1/roads/seg2/seg2.json"})
    result = crawl(_BASE, tmp_path / "dest", fetcher)
    assert set(result.statuses) == set(fetcher.calls)
    assert result.statuses[f"{_BASE}sector-1/roads/seg2/seg2.json"] == 404
    assert result.statuses[f"{_BASE}sector-1/roads/seg1/seg1.json"] == 200
    assert result.documents == 4


def test_crawl_asks_each_url_once(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    fetcher = DirFetcher(root)
    crawl(_BASE, tmp_path / "dest", fetcher)
    assert len(fetcher.calls) == len(set(fetcher.calls))


def test_missing_document_is_absent_from_the_tree(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    dest = tmp_path / "dest"
    crawl(_BASE, dest, DirFetcher(root, missing={"sector-1/catalog.json"}))
    assert not (dest / "sector-1" / "catalog.json").exists()
    # nothing below the missing catalog was reachable, so nothing below it was fetched
    assert not (dest / "sector-1" / "roads").exists()


def test_missing_sidecar_is_absent_not_fatal(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    dest = tmp_path / "dest"
    result = crawl(_BASE, dest, DirFetcher(root, missing={"sector-1/AGENTS.md"}))
    assert not (dest / "sector-1" / "AGENTS.md").exists()
    assert (dest / "sector-1" / "README.md").exists()
    assert result.documents == 5


def test_root_transport_failure_raises(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    with pytest.raises(FetchError, match="connection refused"):
        crawl(_BASE, tmp_path / "dest", DirFetcher(root, broken={"catalog.json"}))


def test_child_transport_failure_is_recorded(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    result = crawl(_BASE, tmp_path / "dest", DirFetcher(root, broken={"sector-1/catalog.json"}))
    assert list(result.errors) == [f"{_BASE}sector-1/catalog.json"]
    assert f"{_BASE}sector-1/catalog.json" not in result.statuses


def test_crawl_follows_absolute_links_under_the_base_only(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = _nested(catalog)

    def absolutize(data: dict[str, Any]) -> None:
        for link in data["links"]:
            if link["rel"] == "child":
                link["href"] = f"{_BASE}sector-1/catalog.json"
        data["links"].append(
            {
                "rel": "child",
                "href": "https://other.example.net/cat/catalog.json",
                "type": "application/json",
            }
        )

    mutate_json(root / "catalog.json", absolutize)
    fetcher = DirFetcher(root)
    result = crawl(_BASE, tmp_path / "dest", fetcher)
    assert f"{_BASE}sector-1/catalog.json" in fetcher.calls
    assert not any("other.example.net" in url for url in fetcher.calls)
    assert result.documents == 5


def test_crawl_ignores_links_that_escape_the_base(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {"rel": "child", "href": "../outside/catalog.json", "type": "application/json"}
        ),
    )
    fetcher = DirFetcher(root)
    crawl(_BASE, tmp_path / "dest", fetcher)
    assert all(url.startswith(_BASE) and ".." not in url for url in fetcher.calls)


@pytest.mark.parametrize(
    "href",
    [
        "https://data.example.org/cat/../../../../tmp/rashid-pwned/PWNED.json",
        "https://data.example.org/cat/sector-1/../../escaped.json",
        "../escaped.json",
        "./sector-1/../../escaped.json",
        "/escaped.json",
        "..%2Fescaped.json",
        "sector-1\\..\\..\\escaped.json",
        "https://data.example.org/cat/",
        "https://data.example.org/cat",
        "https://data.example.org/cat-other/catalog.json",
    ],
)
def test_crawl_never_writes_outside_the_destination(
    catalog: CatalogBuilder, tmp_path: Path, href: str
) -> None:
    """A catalog is hostile input: a link must not choose where its bytes land."""
    root = _nested(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append({"rel": "child", "href": href, "type": "application/json"}),
    )

    class Yes(DirFetcher):
        def get(self, url: str) -> Fetched:
            self.calls.append(url)
            return Fetched(status=200, body=b'{"type": "Catalog", "links": []}')

    dest = tmp_path / "dest"
    before = set(tmp_path.rglob("*"))
    crawl(_BASE, dest, Yes(root))
    created = set(tmp_path.rglob("*")) - before
    assert created and all(p == dest or dest in p.parents for p in created), created
    assert not Path("/tmp/rashid-pwned").exists()


def test_crawl_normalizes_absolute_hrefs_like_relative_ones(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = _nested(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: [
            link.__setitem__("href", f"{_BASE}./sector-1//catalog.json?v=2#top")
            for link in d["links"]
            if link["rel"] == "child"
        ],
    )
    fetcher = DirFetcher(root)
    dest = tmp_path / "dest"
    result = crawl(_BASE, dest, fetcher)
    assert f"{_BASE}sector-1/catalog.json" in fetcher.calls
    assert (dest / "sector-1" / "catalog.json").exists()
    assert result.documents == 5


def test_crawl_follows_json_alternates_for_language_trees(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    from tests.conftest import write_language_trees

    root = write_language_trees(catalog)
    dest = tmp_path / "dest"
    result = crawl(_BASE, dest, DirFetcher(root))
    assert (dest / "ro" / "catalog.json").exists()
    assert (dest / "ro" / "roads" / "collection.json").exists()
    assert result.documents == 4


def test_crawl_does_not_follow_html_alternates(catalog: CatalogBuilder, tmp_path: Path) -> None:
    root = _nested(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {"rel": "alternate", "href": "./index.html", "type": "text/html"}
        ),
    )
    fetcher = DirFetcher(root)
    crawl(_BASE, tmp_path / "dest", fetcher)
    assert f"{_BASE}index.html" not in fetcher.calls


def test_crawl_does_not_descend_non_stac_json(catalog: CatalogBuilder, tmp_path: Path) -> None:
    """An item link pointing at a style file is fetched, judged, and leads nowhere."""
    root = _nested(catalog)
    (root / "sector-1" / "roads" / "style.json").write_text(
        json.dumps({"version": 8, "links": [{"rel": "child", "href": "./x.json"}]})
    )
    mutate_json(
        root / "sector-1" / "roads" / "collection.json",
        lambda d: d["links"].append(
            {"rel": "item", "href": "./style.json", "type": "application/json"}
        ),
    )
    fetcher = DirFetcher(root)
    crawl(_BASE, tmp_path / "dest", fetcher)
    assert f"{_BASE}sector-1/roads/style.json" in fetcher.calls
    assert f"{_BASE}sector-1/roads/x.json" not in fetcher.calls


def test_unparseable_document_is_written_and_not_followed(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = _nested(catalog)
    (root / "sector-1" / "catalog.json").write_text("{not json")
    dest = tmp_path / "dest"
    result = crawl(_BASE, dest, DirFetcher(root))
    assert (dest / "sector-1" / "catalog.json").read_text() == "{not json"
    assert result.documents == 2  # root and the broken file; nothing below it


def test_crawl_caps_the_document_count(catalog: CatalogBuilder, tmp_path: Path) -> None:
    for i in range(6):
        catalog.collection(f"c{i}")
    root = catalog.write()
    result = crawl(_BASE, tmp_path / "dest", DirFetcher(root), max_documents=3)
    assert result.capped
    assert result.documents == 3


def test_crawl_encodes_the_request_url_but_keeps_the_path_as_spelled(
    catalog: CatalogBuilder, tmp_path: Path
) -> None:
    root = _nested(catalog)
    (root / "sector-1").rename(root / "sector 1")
    mutate_json(
        root / "catalog.json",
        lambda d: [
            link.__setitem__("href", "./sector 1/catalog.json")
            for link in d["links"]
            if link["rel"] == "child"
        ],
    )
    fetcher = DirFetcher(root)
    dest = tmp_path / "dest"
    result = crawl(_BASE, dest, DirFetcher(root)) and crawl(_BASE, dest, fetcher)
    assert f"{_BASE}sector%201/catalog.json" in fetcher.calls
    assert (dest / "sector 1" / "catalog.json").exists()
    # the status is keyed by the path as the href spells it, which is how the
    # live pass builds the same URL
    assert f"{_BASE}sector 1/catalog.json" in result.statuses


# --- the runner in URL mode ----------------------------------------------------


def _check_url(root: Path, fetcher: DirFetcher | None = None, **kwargs: Any):  # type: ignore[no-untyped-def]
    fetcher = fetcher or DirFetcher(root)
    kwargs.setdefault("live_prober", StatusProber())
    kwargs.setdefault("data", False)
    return validate(_ROOT_URL, fetcher=fetcher, **kwargs)


def test_url_mode_runs_the_metadata_pass_over_the_crawled_tree(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    report = _check_url(root)
    assert report.files_checked == 5
    ids = {f.rule_id for f in report.findings}
    assert GEN_PARTIAL_TREE in ids
    assert not {i for i in ids if i.startswith("PTL-LNK") or i.startswith("PTL-FIL")}


def test_url_mode_reports_the_same_metadata_findings_as_the_directory(
    catalog: CatalogBuilder,
) -> None:
    root = _nested(catalog)
    mutate_json(root / "sector-1" / "roads" / "collection.json", lambda d: d.pop("license"))
    local = validate(root, data=False)
    remote = _check_url(root, live=False)
    skip = {GEN_PARTIAL_TREE}
    assert {(f.rule_id, f.path) for f in remote.findings if f.rule_id not in skip} == {
        (f.rule_id, f.path) for f in local.findings
    }


def test_url_mode_says_once_what_it_cannot_see(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    report = _check_url(root)
    [note] = [f for f in report.findings if f.rule_id == GEN_PARTIAL_TREE]
    assert note.severity is Severity.WARNING
    assert note.path == "."
    assert "5 document(s)" in note.message
    assert "PTL-LNK-002" in note.message and "PTL-COL-005" in note.message
    assert _ROOT_URL in note.message


def test_url_mode_turns_the_live_pass_on(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    prober = StatusProber()
    report = _check_url(root, live_prober=prober)
    assert LIV_UNAVAILABLE not in {f.rule_id for f in report.findings}
    assert f"{_BASE}sector-1/roads/data.parquet" in prober.head_calls
    assert prober.range_calls  # the range/CORS probes ran under the URL as base


def test_url_mode_live_can_be_turned_off(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    prober = StatusProber()
    report = _check_url(root, live=False, live_prober=prober)
    assert prober.head_calls == [] and prober.range_calls == []
    assert not any(f.rule_id.startswith("PTL-LIV") for f in report.findings)


def test_url_mode_reports_a_document_the_host_lacks(catalog: CatalogBuilder) -> None:
    """The published tree has a child link to a catalog the publisher never uploaded."""
    root = _nested(catalog)
    prober = StatusProber()
    report = _check_url(
        root, DirFetcher(root, missing={"sector-1/catalog.json"}), live_prober=prober
    )
    findings = [f for f in report.findings if f.rule_id == LIV_LINK_TARGET]
    assert [f.path for f in findings] == ["catalog.json"]
    assert "404" in findings[0].message
    # the crawl already knows the answer; the live pass did not ask again
    assert f"{_BASE}sector-1/catalog.json" not in prober.head_calls
    assert not report.passed


def test_url_mode_does_not_head_the_documents_it_fetched(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    fetcher = DirFetcher(root)
    prober = StatusProber()
    _check_url(root, fetcher, live_prober=prober)
    assert not set(fetcher.calls) & set(prober.head_calls)
    # but it does HEAD the assets, which the crawl never fetched
    assert f"{_BASE}sector-1/roads/data.parquet" in prober.head_calls


def test_url_mode_compares_the_self_link_with_the_url(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append(
            {"rel": "self", "href": "https://old.example.org/cat/catalog.json"}
        ),
    )
    report = _check_url(root)
    [finding] = [f for f in report.findings if f.rule_id == LIV_SELF_BASE]
    assert "old.example.org" in finding.message and _BASE in finding.message


def test_url_mode_with_a_matching_self_link_is_quiet(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    mutate_json(
        root / "catalog.json",
        lambda d: d["links"].append({"rel": "self", "href": _ROOT_URL}),
    )
    report = _check_url(root)
    assert LIV_SELF_BASE not in {f.rule_id for f in report.findings}


def test_url_mode_root_404_is_a_missing_root(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    report = _check_url(root, DirFetcher(root, missing={"catalog.json"}))
    [finding] = report.findings
    assert finding.rule_id == GEN_MISSING_ROOT
    assert "404" in finding.message and _ROOT_URL in finding.message
    assert not report.passed


def test_url_mode_root_transport_failure_is_a_missing_root(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    report = _check_url(root, DirFetcher(root, broken={"catalog.json"}))
    [finding] = report.findings
    assert finding.rule_id == GEN_MISSING_ROOT
    assert "connection refused" in finding.message


def test_url_mode_child_transport_failure_is_a_warning(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    report = _check_url(root, DirFetcher(root, broken={"sector-1/catalog.json"}))
    notes = [f for f in report.findings if f.rule_id == GEN_PARTIAL_TREE]
    assert any("connection refused" in f.message for f in notes)


def test_url_mode_reports_a_capped_crawl(catalog: CatalogBuilder) -> None:
    for i in range(6):
        catalog.collection(f"c{i}")
    root = catalog.write()
    report = _check_url(root, max_documents=3, live=False)
    notes = [f for f in report.findings if f.rule_id == GEN_PARTIAL_TREE]
    assert any("stopped at 3 documents" in f.message for f in notes)


def test_url_mode_accepts_the_directory_url(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    report = validate(_BASE.rstrip("/"), fetcher=DirFetcher(root), data=False, live=False)
    assert report.files_checked == 5


def test_url_mode_leaves_no_temporary_tree_behind(
    catalog: CatalogBuilder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    root = _nested(catalog)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _check_url(root, live=False)
    assert list(scratch.iterdir()) == []


def test_url_mode_does_not_report_a_missing_local_partition_or_scene(
    catalog: CatalogBuilder,
) -> None:
    """Rules that read the directory beside a collection stay quiet, not wrong."""
    catalog.collection("scenes")
    root = _nested(catalog)
    (root / "scenes" / "scene-a.tif").write_bytes(b"II*\x00")
    (root / "scenes" / "scene-b.tif").write_bytes(b"II*\x00")
    local = validate(root, data=False)
    assert "PTL-COL-005" in {f.rule_id for f in local.findings}
    remote = _check_url(root, live=False)
    assert "PTL-COL-005" not in {f.rule_id for f in remote.findings}


# --- the reader in URL mode ----------------------------------------------------


def test_reader_resolves_relative_assets_under_the_base(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    graph = CatalogGraph.load(root)
    graph.base_url = _BASE
    node = graph.nodes[PurePosixPath("sector-1/roads/collection.json")]
    reader = FilesystemHttpReader(graph)
    located = reader.locate(node, "./data.parquet")  # not on disk, as after a crawl
    assert located is not None and located.is_remote
    assert located.source == f"{_BASE}sector-1/roads/data.parquet"


def test_reader_prefers_the_file_on_disk(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    graph = CatalogGraph.load(root)
    graph.base_url = _BASE
    node = graph.nodes[PurePosixPath("sector-1/roads/collection.json")]
    (root / "sector-1" / "roads" / "data.parquet").write_bytes(b"PAR1")
    located = FilesystemHttpReader(graph).locate(node, "./data.parquet")
    assert located is not None and not located.is_remote


def test_reader_without_a_base_keeps_a_missing_file_unfetchable(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    graph = CatalogGraph.load(root)
    node = graph.nodes[PurePosixPath("sector-1/roads/collection.json")]
    assert FilesystemHttpReader(graph).locate(node, "./data.parquet") is None


def test_local_only_reader_drops_the_remote_answer_in_url_mode(catalog: CatalogBuilder) -> None:
    root = _nested(catalog)
    graph = CatalogGraph.load(root)
    graph.base_url = _BASE
    node = graph.nodes[PurePosixPath("sector-1/roads/collection.json")]
    assert LocalOnlyReader(graph).locate(node, "./data.parquet") is None


# --- the CLI ---------------------------------------------------------------------


def test_cli_accepts_a_catalog_url(
    catalog: CatalogBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _nested(catalog)
    seen: dict[str, Any] = {}

    def fake_validate(location: Path | str, **kwargs: Any):  # type: ignore[no-untyped-def]
        seen["location"] = location
        seen.update(kwargs)
        return validate(location, fetcher=DirFetcher(root), live_prober=FakeProber(), data=False)

    monkeypatch.setattr("rashid.cli.validate", fake_validate)
    result = CliRunner().invoke(main, ["check", _ROOT_URL])
    assert seen["location"] == _ROOT_URL
    assert seen["live"] is None  # the runner decides the default per mode
    assert "PTL-GEN-002" in result.output


def test_cli_still_rejects_a_missing_path() -> None:
    result = CliRunner().invoke(main, ["check", "/no/such/catalog"])
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_cli_rejects_http_as_a_path() -> None:
    result = CliRunner().invoke(main, ["check", "http://h/cat"])
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_cli_refuses_live_base_url_with_a_url() -> None:
    result = CliRunner().invoke(main, ["check", _ROOT_URL, "--live-base-url", _BASE])
    assert result.exit_code == 2
    assert "not needed" in result.output


def test_cli_refuses_local_data_scope_with_a_url() -> None:
    result = CliRunner().invoke(main, ["check", _ROOT_URL, "--data-scope", "local"])
    assert result.exit_code == 2
    assert "on disk" in result.output


def test_cli_help_names_the_url_form() -> None:
    result = CliRunner().invoke(main, ["check", "--help"])
    assert "published under" in result.output


# --- the default fetcher -------------------------------------------------------


def test_default_fetcher_refuses_non_https() -> None:
    with pytest.raises(ValueError, match="https"):
        _UrllibFetcher().get("http://h/catalog.json")


def test_default_fetcher_sends_a_named_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[Request] = []

    class _Response:
        status = 200

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request: Request, **_kwargs: Any) -> _Response:
        requests.append(request)
        return _Response()

    monkeypatch.setattr("rashid.remote.urlopen", fake_urlopen)
    fetched = _UrllibFetcher().get(_ROOT_URL)
    assert fetched == Fetched(status=200, body=b"{}")
    agent = requests[0].get_header("User-agent")
    assert agent is not None and agent.startswith("rashid/")


def test_default_fetcher_turns_http_errors_into_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.error import HTTPError

    def fake_urlopen(request: Request, **_kwargs: Any) -> None:
        raise HTTPError(_ROOT_URL, 404, "Not Found", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr("rashid.remote.urlopen", fake_urlopen)
    assert _UrllibFetcher().get(_ROOT_URL) == Fetched(status=404)


def test_default_fetcher_raises_fetch_error_without_a_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import URLError

    def fake_urlopen(request: Request, **_kwargs: Any) -> None:
        raise URLError("name or service not known")

    monkeypatch.setattr("rashid.remote.urlopen", fake_urlopen)
    with pytest.raises(FetchError, match="name or service not known"):
        _UrllibFetcher().get(_ROOT_URL)


def test_crawl_result_is_a_plain_record() -> None:
    result = Crawl(base=_BASE)
    assert result.statuses == {} and result.errors == {} and not result.capped
