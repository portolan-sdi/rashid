"""Runner configuration: rule disabling and severity overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rashid.model import Severity


@dataclass(frozen=True)
class RulesConfig:
    """Per-run rule configuration.

    Attributes:
        disabled: Rule ids to skip entirely.
        severity_overrides: Rule id to severity, replacing the rule default.
    """

    disabled: frozenset[str] = frozenset()
    severity_overrides: dict[str, Severity] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RulesConfig:
        """Build from a plain dict, e.g. parsed from JSON/TOML.

        Shape: ``{"disabled": ["PTL-TTL-002"],
        "severity": {"PTL-TTL-002": "error"}}``.
        """
        return cls(
            disabled=frozenset(raw.get("disabled", ())),
            severity_overrides={
                rule_id: Severity(value) for rule_id, value in raw.get("severity", {}).items()
            },
        )
