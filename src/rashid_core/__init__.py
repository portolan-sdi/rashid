"""rashid — validator and linter for Portolan catalogs.

Usage:

    from rashid_core import validate
    report = validate("path/to/catalog")
    if not report.passed:
        for finding in report.errors:
            print(finding.message)
"""

from __future__ import annotations

import logging

from rashid_core import _trace
from rashid_core.config import RulesConfig
from rashid_core.data import validate_data
from rashid_core.live import validate_live
from rashid_core.model import Finding, Severity
from rashid_core.registry import CHECKS, CheckInfo, describe
from rashid_core.report import Report, RuleSummary
from rashid_core.runner import ValidationOptions, validate
from rashid_core.schema import validate_schema
from rashid_core.structural import validate_structural

# A library never prints a log record its caller did not ask for. The
# RASHID_LOG and RASHID_TIMINGS variables are that request (see _trace).
logging.getLogger("rashid").addHandler(logging.NullHandler())
_trace.configure_from_env()

__all__ = [
    "CHECKS",
    "CheckInfo",
    "Finding",
    "Report",
    "RuleSummary",
    "RulesConfig",
    "Severity",
    "ValidationOptions",
    "describe",
    "validate",
    "validate_data",
    "validate_live",
    "validate_schema",
    "validate_structural",
]
