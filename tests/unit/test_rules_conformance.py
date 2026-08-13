from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from rashid import validate
from rashid.model import Severity
from rashid.rules.conformance import _registry
from tests.conftest import PORTOLAN_URI, CatalogBuilder, findings_for, mutate_json

pytestmark = pytest.mark.unit


def test_missing_schema_uri(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("stac_extensions"))
    findings = findings_for(validate(root), "PTL-CNF-001")
    assert len(findings) == 1
    assert findings[0].path == "roads/collection.json"


def test_wrong_host_uri_is_not_recognized(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__(
            "stac_extensions", ["https://example.org/portolan/v0.1.0/schema.json"]
        ),
    )
    findings = findings_for(validate(root), "PTL-CNF-001")
    assert len(findings) == 1


def test_two_portolan_uris_on_one_object(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__(
            "stac_extensions",
            [
                PORTOLAN_URI,
                "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json",
            ],
        ),
    )
    findings = findings_for(validate(root), "PTL-CNF-001")
    assert len(findings) == 1
    assert "exactly one" in findings[0].message


def test_other_extensions_alongside_portolan_pass(catalog: CatalogBuilder) -> None:
    root = catalog.write()
    mutate_json(
        root / "catalog.json",
        lambda d: d.__setitem__(
            "stac_extensions",
            [PORTOLAN_URI, "https://stac-extensions.github.io/file/v2.1.0/schema.json"],
        ),
    )
    assert findings_for(validate(root), "PTL-CNF-001") == []


def test_version_mismatch_with_root_is_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d.__setitem__(
            "stac_extensions",
            ["https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"],
        ),
    )
    report = validate(root)
    findings = findings_for(report, "PTL-CNF-002")
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "v0.2.0" in findings[0].message
    assert report.passed  # a mixed-version catalog remains valid


def test_missing_uri_does_not_also_trigger_mismatch(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("stac_extensions"))
    report = validate(root)
    assert findings_for(report, "PTL-CNF-002") == []


# --- PTL-CNF-003: dataset versioning declares the version extension ----------

_VERSION_URI = "https://stac-extensions.github.io/version/v1.2.0/schema.json"


def test_version_field_without_extension_flags_cnf_003(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.__setitem__("version", "2.1.0"))
    findings = findings_for(validate(root), "PTL-CNF-003")
    assert len(findings) == 1
    assert findings[0].severity is Severity.ERROR
    assert "version extension" in findings[0].message
    assert findings[0].json_pointer == "/version"


def test_version_field_with_extension_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: (
            d.__setitem__("version", "2.1.0"),
            d.__setitem__("stac_extensions", [*d["stac_extensions"], _VERSION_URI]),
        ),
    )
    assert findings_for(validate(root), "PTL-CNF-003") == []


def test_deprecated_on_item_without_extension_flags_cnf_003(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    mutate_json(
        root / "roads" / "seg1" / "seg1.json",
        lambda d: d["properties"].__setitem__("deprecated", True),
    )
    findings = findings_for(validate(root), "PTL-CNF-003")
    assert len(findings) == 1
    assert findings[0].json_pointer == "/properties/deprecated"


def test_no_version_fields_is_clean(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    assert findings_for(validate(catalog.write()), "PTL-CNF-003") == []


# --- PTL-CNF-004: registered extensions carry the registry's pinned version --

_PINNED_RASTER = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
_STALE_RASTER = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"
_UNREGISTERED = "https://stac-extensions.github.io/eo/v1.1.0/schema.json"


def _with_extension(uri: str) -> Callable[[dict[str, Any]], None]:
    """Append ``uri`` to stac_extensions; items are built without the array."""
    return lambda data: data.__setitem__("stac_extensions", [*data.get("stac_extensions", []), uri])


def test_vendored_registry_pins_every_extension() -> None:
    registry = _registry()
    assert len(registry) >= 10, "the vendored extension registry looks truncated"
    assert registry["https://stac-extensions.github.io/raster"] == ("Raster", "v2.0.0")


def test_registry_version_passes(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", _with_extension(_PINNED_RASTER))
    assert findings_for(validate(root), "PTL-CNF-004") == []


def test_stale_extension_version_is_a_warning(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", _with_extension(_STALE_RASTER))
    report = validate(root)
    findings = findings_for(report, "PTL-CNF-004")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity is Severity.WARNING
    assert "Raster" in finding.message
    assert "v1.1.0" in finding.message and "v2.0.0" in finding.message
    assert finding.json_pointer == "/stac_extensions/1"
    assert finding.actual == _STALE_RASTER
    assert finding.expected == _PINNED_RASTER
    assert finding.fix_hint is not None and _PINNED_RASTER in finding.fix_hint
    assert report.passed  # a catalog behind the registry is still valid


def test_version_ahead_of_the_registry_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    ahead = "https://stac-extensions.github.io/table/v9.0.0/schema.json"
    mutate_json(root / "roads" / "collection.json", _with_extension(ahead))
    findings = findings_for(validate(root), "PTL-CNF-004")
    assert len(findings) == 1
    assert findings[0].actual == ahead


def test_unregistered_extension_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", _with_extension(_UNREGISTERED))
    assert findings_for(validate(root), "PTL-CNF-004") == []


def test_unversioned_uri_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        _with_extension("https://stac-extensions.github.io/raster/schema.json"),
    )
    assert findings_for(validate(root), "PTL-CNF-004") == []


def test_portolan_schema_uri_version_is_left_to_cnf_002(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d.__setitem__(
            "stac_extensions",
            ["https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"],
        ),
    )
    report = validate(root)
    assert findings_for(report, "PTL-CNF-004") == []
    assert len(findings_for(report, "PTL-CNF-002")) == 1


def test_stale_extension_on_an_item_is_flagged(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    mutate_json(root / "roads" / "seg1" / "seg1.json", _with_extension(_STALE_RASTER))
    findings = findings_for(validate(root), "PTL-CNF-004")
    assert len(findings) == 1
    assert findings[0].path == "roads/seg1/seg1.json"


def test_non_string_extension_entry_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(
        root / "roads" / "collection.json",
        lambda d: d.__setitem__("stac_extensions", [*d["stac_extensions"], 42]),
    )
    assert findings_for(validate(root), "PTL-CNF-004") == []


def test_missing_stac_extensions_is_ignored(catalog: CatalogBuilder) -> None:
    catalog.collection("roads")
    root = catalog.write()
    mutate_json(root / "roads" / "collection.json", lambda d: d.pop("stac_extensions"))
    assert findings_for(validate(root), "PTL-CNF-004") == []
