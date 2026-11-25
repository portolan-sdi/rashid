"""Every data and live check fires on a valid catalog broken to violate it.

Hypothesis draws a valid data-bearing catalog (``data_live.catalog_specs``),
and each check's breaker from ``data_live`` edits it to violate that check.
The check must then report a finding the valid catalog did not have, so a
check that already notes something about the base, such as the INFO that a
six-row GeoParquet is too short to judge for ordering, still has to say
something new. Needs the ``rashid-core[data]`` extra; skips without it.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("rasterio")
pytest.importorskip("rio_cogeo")

from hypothesis import HealthCheck, given, settings

from rashid_core import validate
from rashid_core.model import Severity
from rashid_core.registry import CHECKS
from rashid_core.report import Report
from tests.conftest import rule_ids
from tests.property.data_live import (
    Spec,
    TreeProber,
    catalog_specs,
    live_options,
    write_catalog,
)
from tests.property.registry import BREAKERS, Target

pytestmark = pytest.mark.integration

STAGES = ("data", "live")
CHECK_IDS = sorted(check_id for check_id, info in CHECKS.items() if info.stage in STAGES)

# Each example writes real assets and validates twice; ten examples per check stay fast.
_EXAMPLES = settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _findings(report: Report) -> set[tuple[str, str, str | None, str]]:
    return {(f.rule_id, f.path, f.json_pointer, f.message) for f in report.findings}


def _valid_report(root: Path) -> Report:
    """The data pass, and the live pass against a host that serves the tree correctly."""
    return validate(root, options=live_options(TreeProber(root), data=True))


@_EXAMPLES
@given(specs=catalog_specs())
def test_base_catalogs_are_valid(specs: list[Spec]) -> None:
    """No data or live check finds more than a note in a base catalog."""
    with TemporaryDirectory() as tmp:
        report = _valid_report(write_catalog(Path(tmp) / "catalog", specs))
    faults = [
        f"{f.rule_id} {f.path} {f.message}"
        for f in report.findings
        if CHECKS[f.rule_id].stage in STAGES and f.severity is not Severity.INFO
    ]
    assert faults == []


@pytest.mark.parametrize("check_id", [c for c in CHECK_IDS if c in BREAKERS])
@_EXAMPLES
@given(specs=catalog_specs())
def test_rule_fires(check_id: str, specs: list[Spec]) -> None:
    with TemporaryDirectory() as tmp:
        root = write_catalog(Path(tmp) / "catalog", specs)
        before = _findings(_valid_report(root))
        options = BREAKERS[check_id](root)
        assert not isinstance(options, Target), "data and live breakers validate the root"
        report = validate(root, options=options)
    assert check_id in rule_ids(report)
    new = {finding for finding in _findings(report) - before if finding[0] == check_id}
    assert new, f"{check_id} reports nothing the valid catalog did not already have"
