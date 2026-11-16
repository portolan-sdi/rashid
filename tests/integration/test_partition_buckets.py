"""Bucket partition globs: anonymous listing, sampling, and refusals by policy.

The end-to-end tests list a local HTTPS server as an S3-compatible service,
named by a ``custom-s3`` storage scheme, so real obstore requests go out.
"""

from __future__ import annotations

from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError

import pytest

pytest.importorskip("pyarrow")

from rashid import _store, _trace, validate
from rashid._href import bucket_glob
from rashid.catalog import CatalogGraph, Node
from rashid.data import partitions
from rashid.data._files import glob_regex, listing_prefix
from rashid.data.reader import Locator
from rashid.model import Severity
from rashid.registry import (
    DAT_GEOPARQUET_VERSION,
    DAT_PARTITION_SCHEMA,
    DAT_UNAVAILABLE,
    DAT_VECTOR_COLUMNS,
)
from tests._https import HttpsServer, Route
from tests.conftest import CatalogBuilder, mutate_json
from tests.integration import _data_assets as assets

# --- glob helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "key", "matches"),
    [
        ("a/*/*.parquet", "a/x/y.parquet", True),
        ("a/*/*.parquet", "a/x/z/y.parquet", False),
        ("a/**/*.parquet", "a/y.parquet", True),
        ("a/**/*.parquet", "a/x/z/y.parquet", True),
        ("a/**", "a/x/z/y.parquet", True),
        ("a/p?.parquet", "a/p1.parquet", True),
        ("a/p?.parquet", "a/p/.parquet", False),
        ("a/[!x]*.parquet", "a/y.parquet", True),
        ("a/[!x]*.parquet", "a/x.parquet", False),
        ("a/b=[0-9].parquet", "a/b=3.parquet", True),
        ("a/b.parquet", "a/bXparquet", False),
        ("a/[x.parquet", "a/[x.parquet", True),
    ],
)
def test_glob_regex(pattern: str, key: str, *, matches: bool) -> None:
    assert bool(glob_regex(pattern).fullmatch(key)) is matches


@pytest.mark.parametrize(
    ("pattern", "prefix"),
    [
        ("andalusia/buildings/*/*.parquet", "andalusia/buildings"),
        ("*.parquet", ""),
        ("parts/b=[0-9]/x.parquet", "parts"),
        ("parts/one.parquet", "parts"),
    ],
)
def test_listing_prefix_stops_before_the_first_wildcard(pattern: str, prefix: str) -> None:
    assert listing_prefix(pattern) == prefix


@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("s3://bkt/parts/*.parquet", ("s3", "bkt", "parts/*.parquet")),
        ("GS://bkt/*.parquet", ("gs", "bkt", "*.parquet")),
        ("az://container/a/**/*.parquet", ("az", "container", "a/**/*.parquet")),
        ("https://host/parts/*.parquet", None),
        ("parts/*.parquet", None),
        ("s3://bkt/", None),
        ("s3:///parts/*.parquet", None),
        ("s3://bkt/parts/*.parquet?x=1", None),
        ("s3://bkt:port/parts/*.parquet", None),
    ],
)
def test_bucket_glob(glob: str, expected: tuple[str, str, str] | None) -> None:
    assert bucket_glob(glob) == expected


def test_local_globs_match_recursively(tmp_path: Path) -> None:
    (tmp_path / "parts" / "a").mkdir(parents=True)
    assets.write_geoparquet(tmp_path / "parts" / "a" / "x.parquet")
    node = Node(
        path=PurePosixPath("collection.json"),
        abs_path=tmp_path / "collection.json",
        kind="collection",
        id="c",
        data={"partition:glob": "parts/**/*.parquet"},
    )
    assert [match for match, _ in partitions.partition_files(node)] == ["parts/a/x.parquet"]


def test_evenly_spaced_keeps_the_ends() -> None:
    items = list(range(100))
    picked = partitions._evenly_spaced(items, 4)
    assert picked == [0, 33, 66, 99]
    assert partitions._evenly_spaced(items, 1) == [0]
    assert partitions._evenly_spaced(items[:3], 4) == [0, 1, 2]


# --- listing a bucket end to end -------------------------------------------------


def _serve(
    server: HttpsServer, tmp_path: Path, names: list[str], **variants: dict[str, Any]
) -> None:
    """Serve one GeoParquet file per name under ``/bkt/parts/`` and list them."""
    listed = []
    for name in names:
        path = tmp_path / f"{name}.parquet"
        assets.write_geoparquet(path, **variants.get(name, {}))
        body = path.read_bytes()
        server.routes[f"/bkt/parts/{name}.parquet"] = Route(body=body)
        listed.append((f"parts/{name}.parquet", len(body)))
    server.buckets["/bkt"] = listed


def _bucket_catalog(tmp_path: Path, base: str, **fields: Any) -> Path:
    catalog = CatalogBuilder(tmp_path / "catalog")
    scheme = {"type": "custom-s3", "platform": base, "bucket": "bkt", "region": "us-east-1"}
    catalog.collection(
        "buildings",
        assets={},
        **{
            "partition:glob": "s3://bkt/parts/*.parquet",
            "storage:schemes": {"local": scheme},
            **fields,
        },
    )
    return catalog.write()


def _dat(root: Path) -> list[Any]:
    return [f for f in validate(root).findings if f.rule_id.startswith("PTL-DAT")]


def test_a_bucket_glob_is_listed_and_its_files_checked(
    https_server: HttpsServer, tmp_path: Path
) -> None:
    _serve(https_server, tmp_path, ["a", "b"], b={"version": "1.0.0"})
    root = _bucket_catalog(tmp_path, https_server.base)
    with _trace.counting() as counts:
        findings = _dat(root)
    version = [f for f in findings if f.rule_id == DAT_GEOPARQUET_VERSION]
    assert len(version) == 1
    assert "parts/b.parquet" in version[0].message
    assert version[0].json_pointer == "/partition:glob"
    # One listing and one ranged GET per footer; the listing gave each size, so no HEAD.
    assert counts.get("http.lists") == 1
    assert counts.get("http.ranges") == 2
    assert "http.heads" not in counts
    assert [seen.method for seen in https_server.seen] == ["GET"] * 3
    assert https_server.seen[0].path == "/bkt?list-type=2&prefix=parts%2F"


def test_sampled_partition_files_feed_the_collection_columns_check(
    https_server: HttpsServer, tmp_path: Path
) -> None:
    """PORTO-FMT-046: a vector partitioned collection documents its columns."""
    _serve(https_server, tmp_path, ["a"])
    root = _bucket_catalog(tmp_path, https_server.base)
    columns = [f for f in _dat(root) if f.rule_id == DAT_VECTOR_COLUMNS]
    assert [(f.severity, f.json_pointer) for f in columns] == [
        (Severity.WARNING, "/partition:glob")
    ]


def test_sampled_partition_schemas_are_compared(https_server: HttpsServer, tmp_path: Path) -> None:
    _serve(https_server, tmp_path, ["a", "b"], b={"columns": {"height": [1.0] * 6}})
    root = _bucket_catalog(tmp_path, https_server.base)
    assert DAT_PARTITION_SCHEMA in {f.rule_id for f in _dat(root)}


def test_a_large_listing_is_sampled(
    https_server: HttpsServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [f"p{i:02}" for i in range(10)]
    _serve(https_server, tmp_path, names)
    monkeypatch.setattr(partitions, "_FOOTER_SAMPLE", 4)
    monkeypatch.setattr(partitions, "_ROW_SAMPLE", 2)
    monkeypatch.setattr(partitions, "_LIST_CAP", 8)
    root = _bucket_catalog(tmp_path, https_server.base)
    with _trace.counting() as counts:
        notes = [f for f in _dat(root) if f.rule_id == DAT_UNAVAILABLE]
    assert [(f.severity, f.message) for f in notes] == [
        (
            Severity.INFO,
            "partition:glob: read the footers of 4 of 8 matched partition files and the"
            " rows of 2, and the listing stopped at 8",
        )
    ]
    assert counts.get("http.ranges") == 4
    read = sorted(s.path for s in https_server.seen if s.path.startswith("/bkt/parts/"))
    assert read == [f"/bkt/parts/{n}.parquet" for n in ("p00", "p02", "p05", "p07")]


@pytest.mark.parametrize(("status", "why"), [(403, "denied"), (404, "not found"), (400, "failed")])
def test_a_failed_listing_warns(
    https_server: HttpsServer, tmp_path: Path, status: int, why: str
) -> None:
    https_server.routes["/bkt?list-type=2&prefix=parts%2F"] = Route(status=status)
    root = _bucket_catalog(tmp_path, https_server.base)
    notes = [f for f in _dat(root) if f.rule_id == DAT_UNAVAILABLE]
    assert len(notes) == 1
    assert notes[0].severity is Severity.WARNING
    assert notes[0].message.startswith(f"partition:glob: listing the glob {why}:")


def test_a_glob_that_matches_nothing_warns(https_server: HttpsServer, tmp_path: Path) -> None:
    https_server.buckets["/bkt"] = [("parts/readme.txt", 3)]
    root = _bucket_catalog(tmp_path, https_server.base)
    notes = [f for f in _dat(root) if f.rule_id == DAT_UNAVAILABLE]
    assert [(f.severity, f.message) for f in notes] == [
        (
            Severity.WARNING,
            "partition:glob: the glob matches none of the 1 object(s) listed under it",
        )
    ]


def test_a_capped_listing_with_no_match_says_it_stopped(
    https_server: HttpsServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    https_server.buckets["/bkt"] = [("parts/a.txt", 1), ("parts/b.txt", 1)]
    monkeypatch.setattr(partitions, "_LIST_CAP", 1)
    root = _bucket_catalog(tmp_path, https_server.base)
    notes = [f for f in _dat(root) if f.rule_id == DAT_UNAVAILABLE]
    assert [f.message for f in notes] == [
        "partition:glob: the glob matches none of the 1 object(s) listed under it,"
        " and the listing stopped there"
    ]


def test_an_unreadable_sampled_file_is_reported(https_server: HttpsServer, tmp_path: Path) -> None:
    https_server.buckets["/bkt"] = [("parts/a.parquet", 100)]
    https_server.routes["/bkt/parts/a.parquet"] = Route(body=b"x" * 100)
    root = _bucket_catalog(tmp_path, https_server.base)
    notes = [f for f in _dat(root) if f.rule_id == DAT_UNAVAILABLE]
    assert len(notes) == 1
    assert "partition file 'parts/a.parquet' data validation could not run" in notes[0].message


# --- where a bucket is, and refusals by policy -----------------------------------


class _Remote:
    """A reader that reads remote files, as ``--data-scope all`` does."""

    def locate(self, _node: Node, href: str) -> Locator | None:
        return Locator(is_remote=True, source=href)

    def stream(self, _node: Node, _href: str) -> None:
        return None


def _node(**fields: Any) -> Node:
    return Node(
        path=PurePosixPath("collection.json"),
        abs_path=Path("/nowhere/collection.json"),
        kind="collection",
        id="c",
        data=fields,
    )


def _access(scheme: str, entry: dict[str, Any] | None) -> Any:
    schemes = {"x": {"bucket": "bkt", **entry}} if entry is not None else None
    node = _node(**({"storage:schemes": schemes} if schemes else {}))
    return partitions._bucket_access(node, None, scheme, "bkt", _Remote())


@pytest.mark.parametrize(
    ("scheme", "entry", "config", "base"),
    [
        (
            "s3",
            {"type": "aws-s3", "region": "eu-west-1"},
            {"region": "eu-west-1"},
            "https://s3.eu-west-1.amazonaws.com/bkt/",
        ),
        ("gs", None, {}, "https://storage.googleapis.com/bkt/"),
        (
            "az",
            {"type": "ms-azure", "account": "acct"},
            {"account_name": "acct"},
            "https://acct.blob.core.windows.net/bkt/",
        ),
        (
            "s3",
            {"type": "custom-s3", "platform": "https://{bucket}.{region}.r2.dev", "region": "eu"},
            {
                "endpoint": "https://bkt.eu.r2.dev",
                "region": "eu",
                "virtual_hosted_style_request": True,
            },
            "https://bkt.eu.r2.dev/",
        ),
        (
            "s3",
            {"type": "custom-s3", "platform": "https://minio.example/"},
            {"endpoint": "https://minio.example"},
            "https://minio.example/bkt/",
        ),
    ],
)
def test_bucket_access(
    scheme: str, entry: dict[str, Any] | None, config: dict[str, Any], base: str
) -> None:
    access = _access(scheme, entry)
    assert (access.config, access.base) == (config, base)


@pytest.mark.parametrize(
    ("scheme", "entry", "reason"),
    [
        ("s3", {"type": "aws-s3", "requester_pays": True}, "the bucket is requester pays"),
        ("az", {"type": "ms-azure"}, "the ms-azure storage scheme names no account"),
        (
            "s3",
            {"type": "custom-s3", "platform": "https://{account}.example"},
            "the custom-s3 platform 'https://{account}.example' does not resolve to an https URL",
        ),
        (
            "s3",
            {"type": "custom-s3", "platform": "http://minio.example"},
            "the custom-s3 platform 'http://minio.example' does not resolve to an https URL",
        ),
        ("az", None, "rashid cannot list a az glob with no storage:schemes entry"),
        (
            "gs",
            {"type": "aws-s3"},
            "rashid cannot list a gs glob with a storage scheme of type 'aws-s3'",
        ),
    ],
)
def test_bucket_access_refusals_are_infos(
    scheme: str, entry: dict[str, Any] | None, reason: str
) -> None:
    refusal = _access(scheme, entry)
    assert (refusal.rule_id, refusal.severity) == (DAT_UNAVAILABLE, Severity.INFO)
    assert refusal.message == f"partition:glob: not listed: {reason}"


def test_an_s3_region_comes_from_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_store, "bucket_region", lambda bucket: "ap-south-1")
    access = _access("s3", None)
    assert access.config == {"region": "ap-south-1"}
    monkeypatch.setattr(_store, "bucket_region", lambda bucket: None)
    warning = _access("s3", None)
    assert warning.severity is Severity.WARNING
    assert warning.message.endswith("no S3 region found for bkt")


def test_a_storage_scheme_on_a_parent_names_the_bucket(tmp_path: Path) -> None:
    catalog = CatalogBuilder(tmp_path)
    catalog.collection("buildings", **{"partition:glob": "s3://bkt/parts/*.parquet"})
    root = catalog.write()
    entry = {"type": "aws-s3", "bucket": "bkt", "region": "eu-west-3"}
    mutate_json(root / "catalog.json", lambda data: data.update({"storage:schemes": {"e": entry}}))
    graph = CatalogGraph.load(root)
    node = next(graph.iter("collection"))
    access = partitions._bucket_access(node, graph, "s3", "bkt", _Remote())
    assert isinstance(access, partitions._Bucket)
    assert access.config == {"region": "eu-west-3"}


# --- the bucket region lookup ------------------------------------------------------


class _Answer:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _message(**values: str) -> Message:
    message = Message()
    for name, value in values.items():
        message[name.replace("_", "-")] = value
    return message


def test_bucket_region_reads_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []

    def answer(url: str, method: str, _headers: dict[str, str]) -> _Answer:
        sent.append((method, url))
        return _Answer({"x-amz-bucket-region": "eu-north-1"})

    monkeypatch.setattr(_store, "probe", answer)
    assert _store.bucket_region("my bucket") == "eu-north-1"
    assert sent == [("HEAD", "https://s3.amazonaws.com/my%20bucket")]


def test_bucket_region_reads_a_denied_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(url: str, _method: str, _headers: dict[str, str]) -> None:
        raise HTTPError(url, 403, "Forbidden", _message(x_amz_bucket_region="us-west-2"), None)

    monkeypatch.setattr(_store, "probe", denied)
    assert _store.bucket_region("bkt") == "us-west-2"


def test_bucket_region_is_none_without_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(*_: object) -> None:
        raise OSError("no route")

    monkeypatch.setattr(_store, "probe", unreachable)
    assert _store.bucket_region("bkt") is None
    monkeypatch.setattr(_store, "probe", lambda *_: _Answer({}))
    assert _store.bucket_region("bkt") is None
