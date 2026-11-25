"""Validation findings and JSON pointers.

A rule emits zero or more findings; the absence of findings is a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Severity(Enum):
    """Severity of a finding.

    ERROR: a MUST requirement is violated; the catalog does not conform.
    WARNING: a SHOULD requirement is violated, or a MUST with an explicit
        spec exception (e.g. schema-URI mismatch from the root).
    INFO: a suggestion the validator cannot fully decide from metadata.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


def json_pointer(*tokens: str | int, prefix: str = "") -> str:
    """Build an RFC 6901 pointer, optionally extending an already-encoded prefix."""
    return prefix + "".join(
        "/" + str(token).replace("~", "~0").replace("/", "~1") for token in tokens
    )


@dataclass(frozen=True)
class Finding:
    """A single defect found in a single object.

    Attributes:
        rule_id: Stable identifier of the rule, e.g. ``PTL-LNK-006``.
        severity: Severity of this finding.
        message: Human-readable description of the defect.
        path: POSIX path of the offending file, relative to the catalog root.
        object_id: STAC ``id`` of the offending object, when known.
        json_pointer: Optional locator within the file, e.g. ``/links/3/href``.
        fix_hint: Optional suggestion for fixing the issue.
        expected: The correct value at that location per the authoritative
            source (e.g. the recomputed checksum, the served byte count), when
            the rule compares two concrete values. JSON-serializable.
        actual: What the object currently declares there. JSON-serializable.
        data: Optional rule-specific machine-usable context for automated
            remediation. JSON-serializable. ``expected``/``actual``/``data``
            make findings actionable without parsing prose; note they also make
            a Finding unhashable in practice when they hold mutable values.
    """

    rule_id: str
    severity: Severity
    message: str
    path: str
    object_id: str | None = None
    json_pointer: str | None = None
    fix_hint: str | None = None
    expected: Any | None = None
    actual: Any | None = None
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict, omitting empty optionals."""
        d: dict[str, Any] = {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
        }
        if self.object_id is not None:
            d["object_id"] = self.object_id
        if self.json_pointer is not None:
            d["json_pointer"] = self.json_pointer
        if self.fix_hint is not None:
            d["fix_hint"] = self.fix_hint
        if self.expected is not None:
            d["expected"] = self.expected
        if self.actual is not None:
            d["actual"] = self.actual
        if self.data is not None:
            d["data"] = self.data
        return d
