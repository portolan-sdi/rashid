"""Production pointers must use the model's token encoder."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_no_fstring_pointers_in_src() -> None:
    copies = []
    for path in (Path(__file__).resolve().parents[2] / "src/rashid").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "json_pointer"
                and isinstance(node.value, ast.JoinedStr)
            ):
                copies.append(f"{path}:{node.value.lineno}")
            if not isinstance(node, ast.JoinedStr):
                continue
            literals = "".join(part.value for part in node.values if isinstance(part, ast.Constant))
            # /vsicurl/ is a GDAL filesystem path, not a JSON pointer.
            first = node.values[0]
            starts_pointer = isinstance(first, ast.Constant) and first.value.startswith("/")
            extends_pointer = (
                isinstance(first, ast.FormattedValue)
                and isinstance(first.value, ast.Name)
                and first.value.id in {"pointer", "prefix"}
                and literals.startswith("/")
            )
            if (starts_pointer or extends_pointer) and not literals.startswith("/vsicurl/"):
                copies.append(f"{path.relative_to(path.parents[2])}:{node.lineno}")
    assert not copies, "hand-built JSON pointers: " + ", ".join(copies)
