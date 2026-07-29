"""Data pass: verify asset bytes against what the metadata declares.

The metadata pass checks that assets *declare* the required fields — an
``href``, a media ``type``, a positive ``file:size``, a well-formed
``file:checksum`` (``PTL-AST-001..004``). None of that opens the asset, so a
catalog can declare a checksum, size, format, or extent that its actual bytes
contradict and still pass. core.md makes several of these a hard MUST:
``file:checksum``/``file:size`` "MUST be regenerated at publish time … so they
always match what is in the bucket", and each format carries a required media
type.

This opt-in pass reads the bytes — local files and remote ``https`` assets — and
reports where they diverge from the claim. Like the structural and schema passes
it is off by default (CLI ``--data``; ``validate(..., data=True)``), reaches the
network, and downgrades to a single WARNING when it cannot run.

The heavy geospatial dependencies (``pyarrow``, ``rasterio``, ``rio-cogeo``,
``pyproj``) are core dependencies but are imported
lazily by :func:`default_validator`, so the core package stays stdlib-only and a
catalog with no data pass installs nothing new.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from rashid.catalog import CatalogGraph, Kind, Node
from rashid.data.reader import AssetReader, FilesystemHttpReader
from rashid.model import Finding, Severity

DAT_UNAVAILABLE = "PTL-DAT-000"
DAT_CHECKSUM = "PTL-DAT-001"
DAT_SIZE = "PTL-DAT-002"
DAT_FORMAT = "PTL-DAT-003"
DAT_COG = "PTL-DAT-004"
DAT_CONSISTENCY = "PTL-DAT-005"
DAT_ORDERING = "PTL-DAT-006"
DAT_ROWGROUP_STATS = "PTL-DAT-007"
DAT_ROWGROUP_SIZE = "PTL-DAT-008"
DAT_COG_STATS = "PTL-DAT-009"
DAT_VALID_PERCENT = "PTL-DAT-010"
DAT_OVERVIEWS = "PTL-DAT-011"
DAT_GEOPARQUET_VERSION = "PTL-DAT-012"
DAT_TILE_SIZE = "PTL-DAT-013"
DAT_PARTITION_SCHEMA = "PTL-DAT-014"
DAT_TABULAR = "PTL-DAT-015"
DAT_MIRROR = "PTL-DAT-016"

# Requirement IDs from the spec's requirements manifest
# (specs/portolan/requirements.yaml) enforced by each check;
# gated by tests/unit/test_spec_coverage.py.
SPEC_IDS: dict[str, tuple[str, ...]] = {
    # Checksum/size verification is the observable half of the
    # regenerated-at-publish-time process MUST (PORTO-CORE-030).
    DAT_CHECKSUM: ("PORTO-CORE-030",),
    DAT_SIZE: ("PORTO-CORE-030",),
    DAT_FORMAT: ("PORTO-CORE-026", "PORTO-FMT-001"),
    DAT_COG: ("PORTO-FMT-001", "PORTO-FMT-023", "PORTO-FMT-024"),
    # The footprint comparison is scoped to geospatial data only, which is
    # how a tabular bbox stays informational (PORTO-FMT-036).
    DAT_CONSISTENCY: ("PORTO-FMT-036",),
    # FMT-043 binds an item mirror to the same three GeoParquet MUSTs as
    # vector data, so the checks that enforce them carry it too.
    DAT_ORDERING: ("PORTO-FMT-006", "PORTO-FMT-043"),
    # ERROR when neither statistics source exists; WARNING when a 2.x file
    # relies on native statistics without the RECOMMENDED covering column.
    DAT_ROWGROUP_STATS: ("PORTO-FMT-007", "PORTO-FMT-008", "PORTO-FMT-043"),
    DAT_ROWGROUP_SIZE: ("PORTO-FMT-009", "PORTO-FMT-043"),
    DAT_COG_STATS: (
        "PORTO-FMT-026",
        "PORTO-FMT-027",
        "PORTO-FMT-028",
        "PORTO-FMT-030",
    ),
    DAT_VALID_PERCENT: ("PORTO-FMT-031",),
    DAT_OVERVIEWS: ("PORTO-FMT-024", "PORTO-FMT-025"),
    DAT_GEOPARQUET_VERSION: ("PORTO-FMT-004",),
    DAT_TILE_SIZE: ("PORTO-FMT-024",),
    DAT_PARTITION_SCHEMA: ("PORTO-FMT-021",),
    DAT_TABULAR: ("PORTO-FMT-037",),
    # A mirror must reproduce the collection's items (PORTO-FMT-042).
    DAT_MIRROR: ("PORTO-FMT-042",),
}

# Assets are declared on collections and items; catalogs carry none.
_DATA_KINDS: tuple[Kind, ...] = ("collection", "item")


@dataclass(frozen=True)
class DataDefect:
    """One byte-vs-metadata mismatch for a single asset.

    A lightweight seed that :func:`validate_data` turns into a :class:`Finding`,
    filling in the object's path and id. ``asset_key`` and ``field`` locate the
    asset within the object (``/assets/<key>[/<field>]``); a node-level defect
    sets ``json_pointer`` directly instead. Two checks do that: the partition
    check, which binds to no single asset, and the consistency check when the
    contradicted ``proj:epsg`` is inherited from the enclosing object rather
    than declared on the asset.
    """

    rule_id: str
    severity: Severity
    message: str
    asset_key: str
    field: str | None = None
    json_pointer: str | None = None
    expected: Any | None = None
    actual: Any | None = None


# A validator inspects one object's assets, given a reader for their bytes, and
# returns the mismatches it found; an empty list means every asset matched.
Validator = Callable[[Node, AssetReader], list[DataDefect]]


def default_validator(graph: CatalogGraph | None = None) -> Validator:
    """Build the byte-verifying validator, importing the geospatial deps lazily.

    The metadata pass never needs these packages; importing :mod:`rashid.data.checks`
    pulls ``pyarrow``/``rasterio``/``pyproj``/``rio_cogeo``. A missing
    extra surfaces here as ``ImportError`` and is downgraded to one WARNING.

    ``graph`` is bound into the returned validator for the one check that needs
    more than a single object: the item mirror must agree with the collection's
    items, which means reading every item beneath the collection
    (``PTL-DAT-016``).
    Called without it, that check is skipped and the rest run unchanged.
    """
    from rashid.data import checks

    if graph is None:
        return checks.check_node
    return partial(checks.check_node, graph=graph)


def validate_data(graph: CatalogGraph, validator: Validator | None = None) -> list[Finding]:
    """Verify every asset's bytes against its declared metadata.

    Returns ``PTL-DAT-00x`` findings for each mismatch. If the validator is
    unavailable (the geospatial stack cannot import) or a call fails
    systemically, returns a single ``PTL-DAT-000`` warning instead of failing the
    run — a systemic failure is reported once, not once per object.
    """
    if validator is None:
        try:
            validator = default_validator(graph)
        except ImportError:
            return [
                Finding(
                    rule_id=DAT_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=(
                        "data validation skipped: the geospatial stack could not be imported "
                        "(needs pyarrow, rasterio, rio-cogeo, and pyproj; pass data=False "
                        "or --no-data to skip byte checks)"
                    ),
                    path=".",
                )
            ]

    reader = FilesystemHttpReader(graph)
    findings: list[Finding] = []
    for node in graph.iter(*_DATA_KINDS):
        if node.parse_error is not None:
            continue
        try:
            defects = validator(node, reader)
        except Exception as exc:  # noqa: BLE001 - any validator failure is systemic
            findings.append(
                Finding(
                    rule_id=DAT_UNAVAILABLE,
                    severity=Severity.WARNING,
                    message=f"data validation could not run: {exc}",
                    path=".",
                )
            )
            return findings
        for defect in defects:
            pointer = defect.json_pointer
            if pointer is None:
                pointer = f"/assets/{defect.asset_key}"
                if defect.field is not None:
                    pointer = f"{pointer}/{defect.field}"
            findings.append(
                Finding(
                    rule_id=defect.rule_id,
                    severity=defect.severity,
                    message=defect.message,
                    path=str(node.path),
                    object_id=node.id,
                    json_pointer=pointer,
                    expected=defect.expected,
                    actual=defect.actual,
                )
            )
    return findings
