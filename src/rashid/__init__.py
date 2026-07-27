"""rashid — validator and linter for Portolan catalogs.

Usage:

    from rashid import validate
    report = validate("path/to/catalog")
    if not report.passed:
        for finding in report.errors:
            print(finding.message)
"""

from __future__ import annotations

from rashid.config import RulesConfig
from rashid.data import validate_data
from rashid.live import validate_live
from rashid.model import Finding, Report, Severity
from rashid.runner import validate
from rashid.schema import validate_schema
from rashid.structural import validate_structural

__all__ = [
    "Finding",
    "Report",
    "RulesConfig",
    "Severity",
    "validate",
    "validate_data",
    "validate_live",
    "validate_schema",
    "validate_structural",
]
