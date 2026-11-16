"""The spec's own profile examples pass the structural, extension, and schema passes.

``tests/fixtures/profile-examples/`` holds the hand-written STAC objects that
portolan-spec checks with its profile schema. They are single files, so this
test lays them out as one tree by their links: the catalog at the root, each
collection in a directory named by its id, and each item under its
collection. Each object's own ``root`` link proves the layout.

The examples are not complete catalogs. They have no README, data files, or
thumbnails, so the metadata rules and the data pass do not apply. The passes
that judge each object against STAC and the profile schema do.
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from rashid import ValidationOptions, validate

pytestmark = pytest.mark.integration

EXAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "profile-examples"

# (rule id, path) -> why the finding is expected. Each entry must still occur,
# so a fixed example fails the test until its entry is removed.
_EXPECTED: dict[tuple[str, str], str] = {}

_PASS_PREFIXES = ("PTL-STR-", "PTL-EXT-", "PTL-SCH-")


def _location(data: dict[str, Any]) -> PurePosixPath:
    kind = data["type"]
    if kind == "Catalog":
        return PurePosixPath("catalog.json")
    if kind == "Collection":
        return PurePosixPath(data["id"], "collection.json")
    return PurePosixPath(data["collection"], data["id"], "item.json")


def _lay_out(dest: Path) -> list[PurePosixPath]:
    placed = []
    for source in sorted(EXAMPLES.glob("*.json")):
        data = json.loads(source.read_bytes())
        location = _location(data)
        root_href = next(link["href"] for link in data["links"] if link["rel"] == "root")
        resolved = posixpath.normpath(posixpath.join(str(location.parent), root_href))
        assert resolved == "catalog.json", f"{source.name}: root link does not fit {location}"
        target = dest / location
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        placed.append(location)
    return placed


def test_the_profile_examples_pass_the_object_passes(tmp_path: Path) -> None:
    placed = _lay_out(tmp_path)
    assert len(placed) == len(list(EXAMPLES.glob("*.json"))) > 0
    options = ValidationOptions(rules=(), structural=True, extensions=True, schema=True, data=False)
    report = validate(tmp_path, options=options)
    found = {
        (f.rule_id, f.path): f.message
        for f in report.findings
        if f.rule_id.startswith(_PASS_PREFIXES)
    }
    unexpected = {key: message for key, message in found.items() if key not in _EXPECTED}
    assert unexpected == {}
    stale = [key for key in _EXPECTED if key not in found]
    assert stale == [], f"remove these fixed entries from _EXPECTED: {stale}"
