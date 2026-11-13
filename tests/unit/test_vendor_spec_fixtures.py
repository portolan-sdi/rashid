"""Profile schema snapshots come from release tags, never the draft tree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import vendor_spec_fixtures as vendor

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Schema test",
            "-c",
            "user.email=schema@example.org",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "spec"
    repo.mkdir()
    _git(repo, "init")
    monkeypatch.setattr(vendor, "BUNDLED_SCHEMAS", tmp_path / "vendored")
    return repo


def test_vendor_reads_both_tag_layouts_without_changing_bytes(spec: Path) -> None:
    expected = {}
    for tag in ("v0.1.0", "v0.1.1", "v0.1.2", "v0.2.0", "v0.3.0"):
        relative = "schema.json" if tag == "v0.3.0" else f"{tag}/schema.json"
        path = spec / "stac/json-schema" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = f'{{ "release": "{tag}" }}\r\n'.encode()
        path.write_bytes(contents)
        expected[tag] = contents
        _git(spec, "add", ".")
        _git(spec, "commit", "-m", f"test: add {tag}")
        _git(spec, "tag", "-a", tag, "-m", tag)
    # The draft and old version folders can change or disappear after release.
    for path in spec.rglob("schema.json"):
        path.unlink()
    (spec / "stac/json-schema/schema.json").write_text('{"draft": true}')
    stale = vendor.BUNDLED_SCHEMAS / "v0.0.0/schema.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")

    assert vendor._vendor_schemas(spec) == list(expected)
    assert {
        path.parent.name: path.read_bytes() for path in vendor.BUNDLED_SCHEMAS.glob("*/schema.json")
    } == expected


def test_single_file_schema_takes_precedence_at_a_tag(spec: Path) -> None:
    schema_dir = spec / "stac/json-schema"
    (schema_dir / "v0.3.0").mkdir(parents=True)
    (schema_dir / "v0.3.0/schema.json").write_bytes(b'{"legacy": true}\n')
    current = b'{"current": true}\n'
    (schema_dir / "schema.json").write_bytes(current)
    _git(spec, "add", ".")
    _git(spec, "commit", "-m", "test: add both layouts")
    _git(spec, "tag", "v0.3.0")

    vendor._vendor_schemas(spec)

    assert (vendor.BUNDLED_SCHEMAS / "v0.3.0/schema.json").read_bytes() == current


@pytest.mark.parametrize("with_tag", [False, True])
def test_missing_tagged_schemas_fail_before_reset(spec: Path, with_tag: bool) -> None:
    (spec / "draft.json").write_text("{}")
    _git(spec, "add", ".")
    _git(spec, "commit", "-m", "test: add draft")
    if with_tag:
        _git(spec, "tag", "v0.3.0")
    vendor.BUNDLED_SCHEMAS.mkdir()
    existing = vendor.BUNDLED_SCHEMAS / "existing.json"
    existing.write_bytes(b"preserve")
    message = "no profile schema found at tag v0.3.0" if with_tag else "fetch the v\\* tags"

    with pytest.raises(SystemExit, match=message):
        vendor._vendor_schemas(spec)

    assert existing.read_bytes() == b"preserve"
