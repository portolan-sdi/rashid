"""Vendor test fixtures from a portolan-spec checkout, reproducibly.

reis is a validator; its strongest regression guard is a real, spec-authored
catalog that it must accept, plus the published profile schema it validates
against. Both live upstream in ``portolan-spec`` and drift as the spec evolves,
so a hand-copied snapshot rots — that rot is exactly the schema-URI-namespace
break this script exists to catch early.

Running it against a spec checkout refreshes four fixture sets and records the
spec commit they came from in ``SPEC_REF``:

- ``tests/fixtures/reference-catalog/`` — the JSON and Markdown of the reference
  catalog, binaries stripped (reis validates metadata, never asset bytes).
- ``tests/fixtures/schema/<vX.Y.Z>/schema.json`` — every published profile
  schema version, discovered on disk, never hardcoded.
- ``tests/fixtures/profile-examples/`` — the profile's own hand-authored STAC
  example objects (the micro-cases its ``check-portolan`` runs).
- ``tests/fixtures/requirements.yaml`` — the spec's requirements manifest, the
  per-requirement ID/severity/enforcement register that
  ``tests/unit/test_spec_coverage.py`` gates every check's ``spec_ids``
  against.

Nothing here assumes a single schema version: the schema set is enumerated from
``stac/json-schema/v*/`` so a new version is vendored the moment it exists.

Usage::

    uv run python scripts/vendor_spec_fixtures.py --spec ../portolan-spec

The nightly spec-sync canary runs this into a temp dir against a fresh clone of
``main`` and diffs the result against the committed fixtures; a non-empty diff
means the spec moved and reis has not caught up.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# Relative to the spec checkout root.
CATALOG_SRC = Path("examples/catalog/reference")
SCHEMA_GLOB = "stac/json-schema/v*/schema.json"
PROFILE_EXAMPLES_SRC = Path("stac/examples")
REQUIREMENTS_SRC = Path("specs/portolan/requirements.yaml")

# Only these extensions are copied out of the catalog tree; the parquet, COG,
# PMTiles, and PNG assets (tens of MB) are never read by the validator.
CATALOG_KEEP = {".json", ".md"}


def _resolve_spec(spec: str | None) -> Path:
    """The spec checkout to vendor from: the flag, then $PORTOLAN_SPEC, then a sibling."""
    for candidate in (spec, os.environ.get("PORTOLAN_SPEC"), REPO_ROOT.parent / "portolan-spec"):
        if candidate:
            path = Path(candidate).expanduser().resolve()
            if (path / "stac").is_dir():
                return path
    sys.exit(
        "no portolan-spec checkout found; pass --spec PATH or set PORTOLAN_SPEC "
        "(looked for a sibling ../portolan-spec)"
    )


def _reset_dir(path: Path) -> None:
    """Empty a fixture directory so upstream removals propagate on re-vendor."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def _vendor_catalog(spec: Path) -> int:
    src = spec / CATALOG_SRC
    if not src.is_dir():
        sys.exit(f"reference catalog not found at {src}")
    dest = FIXTURES / "reference-catalog"
    _reset_dir(dest)
    count = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CATALOG_KEEP:
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        count += 1
    return count


def _vendor_schemas(spec: Path) -> list[str]:
    schemas = sorted(spec.glob(SCHEMA_GLOB))
    if not schemas:
        sys.exit(f"no profile schemas found under {spec / 'stac/json-schema'}")
    dest_root = FIXTURES / "schema"
    _reset_dir(dest_root)
    versions = []
    for schema in schemas:
        version = schema.parent.name  # e.g. "v0.1.0"
        target = dest_root / version / "schema.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(schema, target)
        versions.append(version)
    return versions


def _vendor_profile_examples(spec: Path) -> int:
    src = spec / PROFILE_EXAMPLES_SRC
    dest = FIXTURES / "profile-examples"
    _reset_dir(dest)
    count = 0
    for path in sorted(src.glob("*.json")) if src.is_dir() else []:
        shutil.copyfile(path, dest / path.name)
        count += 1
    return count


def _vendor_requirements(spec: Path) -> int:
    src = spec / REQUIREMENTS_SRC
    if not src.is_file():
        sys.exit(f"requirements manifest not found at {src}")
    dest = FIXTURES / "requirements.yaml"
    shutil.copyfile(src, dest)
    return sum(1 for line in src.read_text(encoding="utf-8").splitlines() if "- id:" in line)


def _spec_ref(spec: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(spec), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", help="path to a portolan-spec checkout")
    args = parser.parse_args()

    spec = _resolve_spec(args.spec)
    files = _vendor_catalog(spec)
    versions = _vendor_schemas(spec)
    examples = _vendor_profile_examples(spec)
    requirements = _vendor_requirements(spec)
    ref = _spec_ref(spec)

    (REPO_ROOT / "SPEC_REF").write_text(
        f"# portolan-spec commit the vendored fixtures were built from.\n"
        f"# Regenerate with: uv run python scripts/vendor_spec_fixtures.py\n"
        f"{ref}\n",
        encoding="utf-8",
    )

    print(f"vendored from {spec} @ {ref[:12]}")
    print(f"  reference-catalog: {files} json/md files")
    print(f"  schema versions:   {', '.join(versions)}")
    print(f"  profile-examples:  {examples} objects")
    print(f"  requirements:      {requirements} manifest entries")


if __name__ == "__main__":
    main()
