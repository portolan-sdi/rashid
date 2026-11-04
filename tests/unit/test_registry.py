"""The check registry covers every id rashid can emit."""

from __future__ import annotations

import pytest

from rashid.registry import CHECKS, DEFAULT_RULES, describe

pytestmark = pytest.mark.unit


def test_every_metadata_rule_is_registered() -> None:
    for rule in DEFAULT_RULES:
        info = CHECKS[rule.id]
        assert info.description == rule.description
        assert info.severity is rule.default_severity
        assert info.spec_ids == rule.spec_ids


def test_unavailable_checks_are_registered() -> None:
    """The ``-000`` ids report a pass could not run and cite no requirement."""
    for check_id in ("PTL-STR-000", "PTL-SCH-000", "PTL-DAT-000", "PTL-LIV-000"):
        assert CHECKS[check_id].spec_ids == ()
        assert CHECKS[check_id].description


def test_no_description_is_empty() -> None:
    blank = [info.id for info in CHECKS.values() if not info.description]
    assert not blank, f"checks registered without a description: {blank}"


def test_ids_follow_the_naming_convention() -> None:
    import re

    bad = [check_id for check_id in CHECKS if not re.fullmatch(r"PTL-[A-Z]{3}-\d{3}", check_id)]
    assert not bad, f"check ids off convention: {bad}"


def test_describe_returns_the_description() -> None:
    assert describe("PTL-AST-003") == CHECKS["PTL-AST-003"].description


def test_describe_tolerates_an_unknown_id() -> None:
    assert describe("PTL-ZZZ-999") == ""


def test_registry_is_read_only() -> None:
    with pytest.raises(TypeError):
        CHECKS["PTL-AST-003"] = None  # type: ignore[index]
