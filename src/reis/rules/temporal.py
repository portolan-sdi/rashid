"""Temporal metadata rules (spec: core.md, Temporal Metadata)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from reis.catalog import CatalogGraph, Node
from reis.model import Finding, Severity
from reis.rule import Rule
from reis.rules._common import parse_rfc3339


def _properties(node: Node) -> dict[str, Any]:
    raw = node.data.get("properties")
    return raw if isinstance(raw, dict) else {}


class DatetimePresentRule(Rule):
    """Items carry a datetime or a start/end interval (SHOULD)."""

    id = "PTL-TMP-001"
    spec_ids = ("PORTO-CORE-042",)
    default_severity = Severity.WARNING
    description = "items should carry a datetime or a start/end interval"
    kinds = ("item",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        props = _properties(node)
        has_datetime = props.get("datetime") is not None
        has_interval = (
            props.get("start_datetime") is not None and props.get("end_datetime") is not None
        )
        if not has_datetime and not has_interval:
            yield self.finding(
                node,
                "item has no datetime and no complete start_datetime/end_datetime interval",
                json_pointer="/properties",
            )


class DatetimeValidRule(Rule):
    """Temporal values that are present parse as RFC 3339, with start <= end."""

    id = "PTL-TMP-002"
    spec_ids = ("PORTO-CORE-019",)
    default_severity = Severity.ERROR
    description = "datetime fields must be RFC 3339 with start_datetime <= end_datetime"
    kinds = ("item",)

    def check(self, node: Node, graph: CatalogGraph) -> Iterable[Finding]:
        props = _properties(node)
        parsed = {}
        for field in ("datetime", "start_datetime", "end_datetime"):
            value = props.get(field)
            if value is None:
                continue
            parsed[field] = parse_rfc3339(value)
            if parsed[field] is None:
                yield self.finding(
                    node,
                    f"'{field}' value {value!r} is not an RFC 3339 date-time",
                    json_pointer=f"/properties/{field}",
                )
        start = parsed.get("start_datetime")
        end = parsed.get("end_datetime")
        if start is not None and end is not None and start > end:
            yield self.finding(
                node,
                "start_datetime is after end_datetime",
                json_pointer="/properties/start_datetime",
            )
