"""Shared asset defects and ordered result aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rashid.model import Severity, json_pointer


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


class DataResults:
    """Finding sources; each build derives shared-field and partition results anew."""

    def __init__(self) -> None:
        self._sources: list[tuple[DataDefect, int | None]] = []

    def extend(self, defects: list[DataDefect], *, partition_total: int | None = None) -> None:
        self._sources.extend((defect, partition_total) for defect in defects)

    def build(self) -> list[DataDefect]:
        groups: dict[tuple[object, ...], list[DataDefect]] = {}
        totals: dict[tuple[object, ...], int] = {}
        signature: tuple[object, ...]
        for index, (defect, total) in enumerate(self._sources):
            if total is not None:
                signature = ("partition", defect.rule_id, defect.severity)
                totals[signature] = total
            elif defect.json_pointer is not None:
                signature = ("field", defect.rule_id, defect.json_pointer, defect.message)
            else:
                signature = ("asset", index)
            groups.setdefault(signature, []).append(defect)
        results: list[DataDefect] = []
        for signature, members in groups.items():
            first = members[0]
            if signature[0] != "partition":
                results.append(first)
                continue
            message = first.message
            if len(members) > 1:
                message = (
                    f"{len(members)} of {totals[signature]} partition files fail this check; "
                    f"e.g. {first.message}"
                )
            results.append(
                DataDefect(
                    first.rule_id,
                    first.severity,
                    message,
                    "",
                    json_pointer=json_pointer("partition:glob"),
                )
            )
        return results
