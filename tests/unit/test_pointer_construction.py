"""Production pointers must use the model's token encoder."""

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_POINTER = re.compile(r"^/(assets|links|properties|item_assets|extent|stac_extensions)(/|$)")


def handbuilt_pointers(source: str) -> list[int]:
    copies = []
    for node in ast.walk(ast.parse(source)):
        value = _pointer_value(node)
        if value is not None and _first_literal(value).startswith("/"):
            copies.append(value.lineno)
        if isinstance(node, ast.JoinedStr):
            if _fstring_pointer(node):
                copies.append(node.lineno)
        elif isinstance(node, ast.BinOp) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"format", "join"}
        ):
            strings = [
                part.value
                for part in ast.walk(node)
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ]
            if any(_POINTER.match(value) for value in strings) or _slash_join(node):
                copies.append(node.lineno)
        elif (
            isinstance(node, ast.keyword)
            and node.arg == "json_pointer"
            and isinstance(node.value, ast.JoinedStr)
        ):
            copies.append(node.value.lineno)
    return sorted(set(copies))


def _pointer_value(node: ast.AST) -> ast.expr | None:
    if isinstance(node, ast.keyword) and node.arg in {"json_pointer", "pointer"}:
        return node.value if _is_builder(node.value) else None
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(
            isinstance(target, ast.Name)
            and target.id in {"json_pointer", "pointer"}
            or isinstance(target, ast.Attribute)
            and target.attr in {"json_pointer", "pointer"}
            for target in targets
        ) and _is_builder(node.value):
            return node.value
    return None


def _is_builder(node: ast.AST | None) -> bool:
    return isinstance(node, (ast.BinOp, ast.JoinedStr)) or (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    )


def _first_literal(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # F-string expressions can contain unrelated keys; inspect the literal parts.
    if isinstance(node, ast.FormattedValue):
        return ""
    return next(
        (literal for child in ast.iter_child_nodes(node) if (literal := _first_literal(child))), ""
    )


def _fstring_pointer(node: ast.JoinedStr) -> bool:
    if not node.values:
        return False
    literals = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
    first = node.values[0]
    starts = isinstance(first, ast.Constant) and bool(_POINTER.match(first.value))
    extends = (
        isinstance(first, ast.FormattedValue)
        and isinstance(first.value, ast.Name)
        and first.value.id in {"pointer", "prefix"}
        and literals.startswith("/")
    )
    # /vsicurl/ is a GDAL filesystem path.
    return (starts or extends) and not literals.startswith("/vsicurl/")


def _slash_join(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and node.func.value.value == "/"
    )


def test_no_handbuilt_pointers_in_src() -> None:
    copies = []
    root = Path(__file__).resolve().parents[2]
    for path in (root / "src/rashid").rglob("*.py"):
        if path == root / "src/rashid/model.py":
            continue  # the RFC 6901 encoder owns construction
        copies.extend(
            f"{path.relative_to(root)}:{line}"
            for line in handbuilt_pointers(path.read_text(encoding="utf-8"))
        )
    assert not copies, "hand-built JSON pointers: " + ", ".join(copies)


@pytest.mark.parametrize(
    "token", ["assets", "links", "properties", "item_assets", "extent", "stac_extensions"]
)
@pytest.mark.parametrize(
    "expression",
    [
        'f"/{token}/{{key}}"',
        '"/{token}/" + key',
        '"/{token}/%s" % key',
        '"/{token}/{{}}".format(key)',
        '"/".join(["", "{token}", key])',
    ],
)
def test_scan_rejects_each_construction(token: str, expression: str) -> None:
    assert handbuilt_pointers("pointer = " + expression.format(token=token)) == [1]


@pytest.mark.parametrize(
    "expression",
    ['json_pointer("assets", key)', 'f"/vsicurl/{url}"', '"https://host/" + key', 'f""'],
)
def test_scan_accepts_owner_calls_and_non_pointers(expression: str) -> None:
    assert handbuilt_pointers("value = " + expression) == []


@pytest.mark.parametrize(
    "sink", ["pointer = {}", "json_pointer: str = {}", "Finding(json_pointer={})", "f(pointer={})"]
)
@pytest.mark.parametrize(
    "expression",
    [
        '"/" + key',
        'PREFIX + "/" + key',
        '"/%s" % key',
        '(PREFIX + "/%s") % key',
        '"/{}".format(key)',
        '(PREFIX + "/{}").format(key)',
        'f"/{key}"',
        'f"{PREFIX}/{key}"',
    ],
)
def test_slash_prefixes_in_pointer_values_are_rejected(sink: str, expression: str) -> None:
    assert handbuilt_pointers(sink.format(expression)) == [1]


@pytest.mark.parametrize(
    "source",
    [
        'path = "/" + key',
        'path = PREFIX + "/" + key',
        'path = f"/{key}"',
        'path = f"{PREFIX}/{key}"',
        'url = "/{}".format(key)',
        'message = "/%s" % key',
    ],
)
def test_slash_prefixes_outside_pointer_values_are_not_reported(source: str) -> None:
    assert handbuilt_pointers(source) == []
