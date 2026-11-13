"""Disabling a networked pass uses its registered gate members."""

from __future__ import annotations

import pytest

import rashid.data as data_pass
from rashid import ValidationOptions
from rashid.catalog import Node
from rashid.config import RulesConfig
from rashid.data import DataDefect
from rashid.data.reader import AssetReader
from rashid.registry import CHECKS, DAT_MIRROR, DAT_UNAVAILABLE, LIV_UNAVAILABLE, check_ids
from rashid.runner import validate
from tests.conftest import CatalogBuilder

pytestmark = pytest.mark.unit


def _rules_of(prefix: str) -> frozenset[str]:
    """Every registered rule under ``prefix``, without the pass's ``-000`` sentinel.

    A rule need not cite a requirement (``PTL-DAT-022`` is an uncited hint),
    but the ``-000`` finding only reports that the pass could not run.
    """
    return frozenset(
        info.id
        for info in CHECKS.values()
        if info.id.startswith(prefix) and not info.id.endswith("-000")
    )


def test_data_gate_lists_every_data_rule() -> None:
    assert _rules_of("PTL-DAT-") == check_ids("data", enables_pass_only=True)


def test_live_gate_lists_every_live_rule() -> None:
    assert _rules_of("PTL-LIV-") == check_ids("live", enables_pass_only=True)


def test_degradation_reports_are_not_gate_members() -> None:
    """``-000`` is not a rule, so disabling every rule cannot require it."""
    assert DAT_UNAVAILABLE not in check_ids("data", enables_pass_only=True)
    assert LIV_UNAVAILABLE not in check_ids("live", enables_pass_only=True)


def _spy() -> tuple[list[str], data_pass.Validator]:
    seen: list[str] = []

    def check(node: Node, reader: AssetReader) -> list[DataDefect]:  # noqa: ARG001 - data validator callback requires node and reader
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
        options=ValidationOptions(
            config=RulesConfig(disabled=check_ids("data", enables_pass_only=True) - {DAT_MIRROR}),
            structural=False,
            data_validator=spy,
        ),
    )

    assert seen, "the data pass was skipped while PTL-DAT-016 was still enabled"


def test_disabling_every_rule_still_skips_the_pass(catalog: CatalogBuilder) -> None:
    catalog.collection("roads").item("seg1")
    root = catalog.write()
    seen, spy = _spy()

    validate(
        root,
        options=ValidationOptions(
            config=RulesConfig(disabled=check_ids("data", enables_pass_only=True)),
            structural=False,
            data_validator=spy,
        ),
    )

    assert seen == []
