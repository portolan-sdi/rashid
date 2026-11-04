"""Schema refreshes retain explicit compatibility versions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_vendor_keeps_contacts_compatibility_and_prunes_other_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts/vendor_stac_schemas.py"
    spec = importlib.util.spec_from_file_location("vendor_stac_schemas", script)
    assert spec is not None and spec.loader is not None
    vendor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vendor)
    monkeypatch.setattr(vendor, "_fetch", lambda url: {})
    monkeypatch.setattr(vendor, "_EXTENSION_VENDOR_DIR", tmp_path)
    fetched = vendor.crawl()
    current = "https://stac-extensions.github.io/contacts/v1.0.0/schema.json"
    previous = "https://stac-extensions.github.io/contacts/v0.1.1/schema.json"
    assert current in fetched and previous in fetched
    for uri in (current, previous):
        dest = vendor._dest(uri)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}")
    stale = tmp_path / "stale.json"
    stale.write_text("{}")
    assert vendor.prune(fetched) == [stale]
