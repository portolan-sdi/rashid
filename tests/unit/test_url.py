"""URL identity follows the object store's transport boundaries."""

from pathlib import Path

import pytest

from rashid._url import object_location

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "left,right,equal",
    [
        ("https://HOST:443/a", "https://host/a#fragment", True),
        ("http://host:80/a", "http://host/a", True),
        ("https://[::1]:443/a", "https://[::1]/a", True),
        ("https://host:444/a", "https://host/a", False),
        ("https://host:bad/a", "https://host/a", False),
        ("https://host/%61", "https://host/a", True),
        ("https://host/a%2fb", "https://host/a%2Fb", False),
        ("https://host/a%3Bb", "https://host/a;b", True),
        ("https://host/a%20b", "https://host/a b", True),
        ("https://host/%E6%9D%B1%E4%BA%AC", "https://host/東京", True),
        ("https://host/a%3Bb?x=1", "https://host/a;b?x=1", False),
        ("https://host/%61?x=1", "https://host/a?x=1", False),
        ("https://host/a%2Fb", "https://host/a/b", False),
        ("https://host/a%3Fb", "https://host/a?b", False),
        ("https://host/a%23b", "https://host/a#b", False),
        ("https://host/a//b", "https://host/a/b", False),
        ("https://host/a/../b", "https://host/b", False),
        ("https://host/a/%2e/b", "https://host/a/./b", False),
        ("https://host/a?x=1", "https://host/a?x=2", False),
    ],
)
def test_object_location(left: str, right: str, equal: bool) -> None:
    assert (object_location(left) == object_location(right)) is equal


def test_file_location(tmp_path: Path) -> None:
    path = tmp_path / "a b.json"
    assert object_location(path.as_uri()) == object_location(
        str(tmp_path.as_uri()) + "/x/../a%20b.json"
    )
