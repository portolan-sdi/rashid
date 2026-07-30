"""The pass-skip gates must list every rule their pass can raise.

``runner._DATA_RULE_IDS`` and ``runner._LIVE_RULE_IDS`` exist so that disabling
every rule of a networked pass skips the pass itself. An id missing from either
set is invisible in the worst way: the caller disables the ids it knows about,
the gate sees a superset of them, and the pass is skipped taking a rule the
caller never disabled with it. ``PTL-DAT-016`` was dropped that way (#86).

The expected ids are derived, never listed. :mod:`rashid.registry` documents
itself as the single enumeration of every check rashid can emit and raises at
import when a pass declares a rule it does not describe, so ``CHECKS`` is the
strongest source of truth available in-process. Within one pass's id prefix the
degradation report (``-000``) is exactly the check that cites no spec
requirement — it reports that the pass could not run rather than enforcing
anything — so ``spec_ids`` separates rule from report without naming an id
here. A gate against drift that has to be edited when a rule is added would be
the same gate that just failed.
"""

from __future__ import annotations

import pytest

import rashid.data as data_pass
import rashid.live as live_pass
import rashid.runner as runner
from rashid.catalog import Node
from rashid.config import RulesConfig
from rashid.data import DAT_MIRROR, DAT_UNAVAILABLE, DataDefect
from rashid.data.reader import AssetReader
from rashid.live import LIV_UNAVAILABLE
from rashid.registry import CHECKS
from rashid.runner import validate
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit


def _rules_of(prefix: str) -> frozenset[str]:
    """Every registered check under ``prefix`` that enforces a requirement."""
    return frozenset(
        info.id for info in CHECKS.values() if info.id.startswith(prefix) and info.spec_ids
    )


def test_data_gate_lists_every_data_rule() -> None:
    assert runner._DATA_RULE_IDS == _rules_of("PTL-DAT-")


def test_live_gate_lists_every_live_rule() -> None:
    assert runner._LIVE_RULE_IDS == _rules_of("PTL-LIV-")


def test_gates_agree_with_the_pass_modules() -> None:
    """A second derivation, from the passes' own spec registries.

    The registry join and each pass's ``SPEC_IDS`` are written in different
    files, so a rule added to one and forgotten in the other fails here rather
    than agreeing with a gate that is wrong twice.
    """
    assert runner._DATA_RULE_IDS == frozenset(data_pass.SPEC_IDS)
    assert runner._LIVE_RULE_IDS == frozenset(live_pass.SPEC_IDS)


def test_degradation_reports_are_not_gate_members() -> None:
    """``-000`` is not a rule, so disabling every rule cannot require it."""
    assert DAT_UNAVAILABLE not in runner._DATA_RULE_IDS
    assert LIV_UNAVAILABLE not in runner._LIVE_RULE_IDS


def _spy() -> tuple[list[str], data_pass.Validator]:
    seen: list[str] = []

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:
        seen.append(str(node.path))
        return []

    return seen, check


def test_mirror_rule_keeps_the_pass_alive(catalog: CatalogBuilder) -> None:
    """Disabling every rule but ``PTL-DAT-016`` must still run the pass.

    The concrete regression: with the mirror rule missing from the gate, these
    fifteen ids skipped a sixteenth the caller had left enabled.
    """
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    seen, spy = _spy()

    validate(
        root,
        config=RulesConfig(disabled=runner._DATA_RULE_IDS - {DAT_MIRROR}),
        structural=False,
        data_validator=spy,
    )

    assert seen, "the data pass was skipped while PTL-DAT-016 was still enabled"


def test_disabling_every_rule_still_skips_the_pass(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    seen, spy = _spy()

    validate(
        root,
        config=RulesConfig(disabled=runner._DATA_RULE_IDS),
        structural=False,
        data_validator=spy,
    )

    assert seen == []
