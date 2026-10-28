"""The seeded scenarios build the same bytes every time, and refuse unknown names."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import scenarios

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", ["metadata", "data"])
def test_same_seed_gives_identical_trees(name: str, tmp_path: Path) -> None:
    if name == "data":
        pytest.importorskip("pyarrow")
        pytest.importorskip("rasterio")
    first = scenarios.build(name, "small", tmp_path / "a")
    second = scenarios.build(name, "small", tmp_path / "b")
    assert scenarios.tree_digest(first) == scenarios.tree_digest(second)


def test_another_seed_gives_another_tree(tmp_path: Path) -> None:
    first = scenarios.build("metadata", "small", tmp_path / "a", seed=0)
    second = scenarios.build("metadata", "small", tmp_path / "b", seed=1)
    assert scenarios.tree_digest(first) != scenarios.tree_digest(second)


def test_remote_serves_the_metadata_tree(tmp_path: Path) -> None:
    assert scenarios.cached("remote", "small", cache_dir=tmp_path) == scenarios.cached(
        "metadata", "small", cache_dir=tmp_path
    )


def test_remote_data_serves_the_data_tree(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    assert scenarios.cached("remote-data", "small", cache_dir=tmp_path) == scenarios.cached(
        "data", "small", cache_dir=tmp_path
    )


def test_remote_data_target_serves_every_file_over_https(tmp_path: Path) -> None:
    """The child server answers each file of the tree at its relative path."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("rasterio")
    from rashid import _store

    root = scenarios.cached("data", "small", cache_dir=tmp_path)
    with scenarios.target("remote-data", "small", cache_dir=tmp_path) as target:
        assert target.options == {"data": True, "live": False}
        tif = root / "layers" / "raster-000" / "data.tif"
        assert _store.get(target.server.url("layers/raster-000/data.tif")) == tif.read_bytes()
        assert [(s.method, s.path) for s in target.server.seen()] == [
            ("GET", "/layers/raster-000/data.tif")
        ]
        target.server.reset()
        assert target.server.seen() == []
        assert target.server.peak_in_flight() == 0


def test_cached_reuses_a_complete_tree_and_rebuilds_a_partial_one(tmp_path: Path) -> None:
    root = scenarios.cached("metadata", "small", cache_dir=tmp_path)
    marker = root / "marker"
    marker.write_text("kept", encoding="utf-8")
    assert scenarios.cached("metadata", "small", cache_dir=tmp_path) == root
    assert marker.exists()
    (root.parent / ".complete").unlink()
    assert scenarios.cached("metadata", "small", cache_dir=tmp_path) == root
    assert not marker.exists()


@pytest.mark.parametrize(("name", "size"), [("huge", "small"), ("metadata", "medium")])
def test_build_refuses_unknown_names(name: str, size: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be one of"):
        scenarios.build(name, size, tmp_path / "x")


def test_describe_names_every_scenario_and_size() -> None:
    text = scenarios.describe()
    for name in scenarios.NAMES:
        for size in scenarios.SIZES:
            assert f"{name}/{size}:" in text
