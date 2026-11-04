"""Registration invariants at the owner; public behavior is captured through the CLI."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from rashid import registry
from rashid.model import Severity
from rashid.rule import Rule

pytestmark = pytest.mark.unit


def test_duplicate_pass_ids_are_rejected() -> None:
    check = registry.CHECKS["PTL-DAT-002"]
    with pytest.raises(registry.RegistryError, match="duplicate.*PTL-DAT-002"):
        registry._build((), (check, check))


def test_duplicate_metadata_ids_are_rejected() -> None:
    rule = registry.DEFAULT_RULES[0]
    with pytest.raises(registry.RegistryError, match=f"duplicate.*{rule.id}"):
        registry._build((rule, rule), ())


def test_metadata_and_pass_ids_cannot_collide() -> None:
    rule = registry.DEFAULT_RULES[0]
    check = replace(registry.CHECKS[rule.id], pass_name="data")
    with pytest.raises(registry.RegistryError, match=f"duplicate.*{rule.id}"):
        registry._build((rule,), (check,))


@pytest.mark.parametrize("spec_ids", [(), ("PORTO-CORE-030",)])
def test_registry_requires_a_description(spec_ids: tuple[str, ...]) -> None:
    check = replace(registry.CHECKS["PTL-DAT-002"], description="", spec_ids=spec_ids)
    with pytest.raises(registry.RegistryError, match="has no description"):
        registry._build((), (check,))


def test_pass_id_without_spec_metadata_is_rejected() -> None:
    check = replace(registry.CHECKS["PTL-DAT-002"], spec_ids=())
    with pytest.raises(registry.RegistryError, match="PTL-DAT-002.*spec metadata"):
        registry._build((), (check,))


def test_metadata_id_without_spec_metadata_is_rejected() -> None:
    class Uncited(Rule):
        id = "PTL-TST-999"
        description = "a check with no spec metadata"
        default_severity = Severity.ERROR

    with pytest.raises(registry.RegistryError, match="PTL-TST-999.*spec metadata"):
        registry._build((Uncited(),), ())


def test_every_registered_check_is_reachable_from_exactly_one_pass() -> None:
    passes = ("metadata", "runner", "structural", "extensions", "schema", "data", "live")
    reached = Counter(rule_id for name in passes for rule_id in registry.check_ids(name))
    assert reached == Counter(dict.fromkeys(registry.CHECKS, 1))
    assert registry.check_ids("metadata") == {rule.id for rule in registry.DEFAULT_RULES}


def test_diagnostic_ids_need_an_explicit_spec_reason() -> None:
    check = registry.CHECKS["PTL-DAT-000"]
    assert check.spec_note
    assert registry._build((), (check,))[check.id] == check
    with pytest.raises(registry.RegistryError, match="PTL-DAT-000.*spec metadata"):
        registry._build((), (replace(check, spec_note=None),))


@pytest.mark.parametrize(
    ("pass_name", "expected"),
    [
        ("structural", {"PTL-STR-001"}),
        ("schema", {"PTL-SCH-001"}),
        ("extensions", {"PTL-EXT-001"}),
    ],
)
def test_schema_pass_gates_exclude_diagnostics(
    pass_name: registry.PassName, expected: set[str]
) -> None:
    assert registry.check_ids(pass_name, enables_pass_only=True) == expected


@pytest.mark.parametrize("pass_name", ["data", "live"])
def test_networked_pass_gates_include_every_check_except_availability(
    pass_name: registry.PassName,
) -> None:
    expected = {
        info.id
        for info in registry.CHECKS.values()
        if info.pass_name == pass_name and info.spec_ids
    }
    assert registry.check_ids(pass_name, enables_pass_only=True) == expected
    assert len(registry.check_ids(pass_name) - expected) == 1
