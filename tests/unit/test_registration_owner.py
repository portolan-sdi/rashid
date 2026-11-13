"""Registration invariants at the owner; public behavior is captured through the CLI."""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import Path

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
    check = replace(registry.CHECKS[rule.id], stage="data")
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


def test_registered_ids_equal_ids_emitted_in_src() -> None:
    """Read executable AST nodes, including registry constants used by passes."""
    emitted = set()
    for path in (Path(__file__).resolve().parents[2] / "src/rashid").rglob("*.py"):
        if path.name == "registry.py":
            continue  # declarations do not prove an emitter exists
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {
            alias.asname or alias.name: getattr(registry, alias.name, None)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "rashid.registry"
            for alias in node.names
        }
        for node in ast.walk(tree):
            value = None
            if isinstance(node, ast.Constant):
                value = node.value
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                value = aliases.get(node.id)
            if isinstance(value, str) and re.fullmatch(r"PTL-[A-Z]{3}-\d{3}", value):
                emitted.add(value)
    assert emitted == set(registry.CHECKS)
    assert registry.check_ids("metadata") == {rule.id for rule in registry.DEFAULT_RULES}


def test_pass_checks_use_the_id_constants_once() -> None:
    """Pass identity is declared once, then referenced by CheckInfo and emitters."""
    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and re.fullmatch(r"PTL-[A-Z]{3}-\d{3}", node.value.value)
    }
    declarations = [
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CheckInfo"
        and any(keyword.arg == "stage" for keyword in node.keywords)
    ]
    assert all(isinstance(node, ast.Name) for node in declarations)
    assert sorted(node.id for node in declarations) == sorted(constants)
    assert len(set(constants.values())) == len(constants)


def test_diagnostic_ids_need_an_explicit_spec_reason() -> None:
    check = registry.CHECKS["PTL-DAT-000"]
    assert check.spec_note
    assert registry._build((), (check,))[check.id] == check
    with pytest.raises(registry.RegistryError, match="PTL-DAT-000.*spec metadata"):
        registry._build((), (replace(check, spec_note=None),))


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("structural", {"PTL-STR-001"}),
        ("schema", {"PTL-SCH-001"}),
        ("extensions", {"PTL-EXT-001"}),
    ],
)
def test_schema_pass_gates_exclude_diagnostics(
    stage: registry.PassName, expected: set[str]
) -> None:
    assert registry.check_ids(stage, enables_pass_only=True) == expected


@pytest.mark.parametrize("stage", ["data", "live"])
def test_networked_pass_gates_include_every_check_except_availability(
    stage: registry.PassName,
) -> None:
    expected = {
        info.id for info in registry.CHECKS.values() if info.stage == stage and info.spec_ids
    }
    assert registry.check_ids(stage, enables_pass_only=True) == expected
    assert len(registry.check_ids(stage) - expected) == 1
