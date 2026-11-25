"""Each rule fires on a catalog built to break it.

Every check ID in ``rashid_core.registry.CHECKS`` has a breaker in
``tests/property/registry.BREAKERS`` (``breakers_core.py`` for the metadata,
runner, structural, extension and schema stages; the data and live stages in
``test_rules_fire_data_live.py``) or a reason in ``UNBREAKABLE``. The breaker
edits a generated valid catalog, and the property asserts its ID is reported.
"""

from __future__ import annotations

import importlib
import importlib.util
import tempfile
from pathlib import Path

import pytest
from hypothesis import given

from rashid_core import ValidationOptions, validate
from rashid_core.registry import CHECKS
from tests.conftest import rule_ids
from tests.property import breakers_core  # noqa: F401 - registers the core breakers
from tests.property.registry import BREAKERS, UNBREAKABLE, Target
from tests.property.strategies import CatalogPlan, catalogs

pytestmark = pytest.mark.integration

_DATA_LIVE = "tests.property.test_rules_fire_data_live"

# shortcut: until test_rules_fire_data_live.py lands, the data and live stages
# have no breakers and the gate covers the other stages only; drop the
# fallback when it does.
if importlib.util.find_spec(_DATA_LIVE) is not None:
    importlib.import_module(_DATA_LIVE)
    _EXPECTED = set(CHECKS)
else:
    _EXPECTED = {cid for cid, info in CHECKS.items() if info.stage not in {"data", "live"}}


def test_every_check_has_a_breaker_or_a_reason() -> None:
    assert set(BREAKERS) | set(UNBREAKABLE) == _EXPECTED
    assert not set(BREAKERS) & set(UNBREAKABLE)
    assert all(reason.strip() for reason in UNBREAKABLE.values())


@pytest.mark.parametrize("check_id", sorted(BREAKERS))
@given(plan=catalogs(min_items=1))
def test_the_rule_fires(check_id: str, plan: CatalogPlan) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = plan.write(Path(tmp) / "catalog")
        broken = BREAKERS[check_id](root)
        if not isinstance(broken, Target):
            broken = Target(root, broken or ValidationOptions())
        report = validate(broken.location, options=broken.options)
        assert check_id in rule_ids(report), sorted(rule_ids(report))
