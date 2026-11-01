"""Fail on a ``#`` comment, or on any docstring but the module's, in ``src/``, ``scripts/`` and ``tests/``.

Generated files follow the same rule. The rules are CONTRIBUTING.md's code rules 1 and 2.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKED_DIRECTORIES = ("src", "scripts", "tests")
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def findings(source: bytes) -> list[tuple[int, str]]:
    comments = [
        (token.start[0], "comment")
        for token in tokenize.tokenize(io.BytesIO(source).readline)
        if token.type == tokenize.COMMENT
    ]
    docstrings = [
        (node.body[0].lineno, "docstring outside the module docstring")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, DEFINITIONS) and ast.get_docstring(node, clean=False) is not None
    ]
    return sorted(comments + docstrings)


def main() -> int:
    found = [
        (path, line, kind)
        for directory in CHECKED_DIRECTORIES
        for path in sorted((ROOT / directory).rglob("*.py"))
        for line, kind in findings(path.read_bytes())
    ]
    for path, line, kind in found:
        print(f"{path.relative_to(ROOT).as_posix()}:{line}: {kind}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
