"""Profile schema snapshots come from release tags, never the draft tree."""

from __future__ import annotations

import json
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
        contents = f'{{ "$id": "https://schemas.portolan-sdi.org/portolan/{tag}/schema.json#" }}\r\n'.encode()
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
    current = b'{"$id": "https://schemas.portolan-sdi.org/portolan/v0.3.0/schema.json#"}\n'
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


def test_only_final_release_tags_are_vendored_in_version_order(spec: Path) -> None:
    schema = spec / "stac/json-schema/schema.json"
    schema.parent.mkdir(parents=True)
    for tag in ("v0.10.0", "v0.2.0", "v0.3.0-rc.1", "v1", "v2.0.0+build"):
        schema.write_text(
            json.dumps({"$id": f"https://schemas.portolan-sdi.org/portolan/{tag}/schema.json#"})
        )
        _git(spec, "add", ".")
        _git(spec, "commit", "-m", f"test: add {tag}")
        _git(spec, "tag", tag)

    assert vendor._vendor_schemas(spec) == ["v0.2.0", "v0.10.0"]
    assert sorted(path.name for path in vendor.BUNDLED_SCHEMAS.iterdir()) == ["v0.10.0", "v0.2.0"]


@pytest.mark.parametrize(
    "contents",
    [b'{"$id": "https://example.org/portolan/v0.1.0/schema.json"}', b"{}", b"[]", b"{bad"],
)
def test_wrong_schema_id_fails_before_reset(spec: Path, contents: bytes) -> None:
    schema = spec / "stac/json-schema/schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes(contents)
    _git(spec, "add", ".")
    _git(spec, "commit", "-m", "test: add invalid tagged schema")
    _git(spec, "tag", "v0.2.0")
    vendor.BUNDLED_SCHEMAS.mkdir()
    previous = vendor.BUNDLED_SCHEMAS / "previous.json"
    previous.write_bytes(b"preserve")

    with pytest.raises(SystemExit, match="(?:at tag v0.2.0|tag v0.2.0 must contain)"):
        vendor._vendor_schemas(spec)

    assert previous.read_bytes() == b"preserve"


def test_missing_draft_fails_with_its_source_path(spec: Path) -> None:
    with pytest.raises(SystemExit, match="no draft profile schema found under"):
        vendor._vendor_draft_schema(spec)
