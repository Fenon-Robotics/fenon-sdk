"""Fail when a Python file in ``src/``, ``scripts/`` or ``tests/`` contains a ``#`` comment."""

from __future__ import annotations

import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKED_DIRECTORIES = ("src", "scripts", "tests")


def comment_lines(path: Path) -> list[int]:
    """Return the line numbers of every comment token in a Python file."""
    with path.open("rb") as stream:
        return [
            token.start[0] for token in tokenize.tokenize(stream.readline) if token.type == tokenize.COMMENT
        ]


def main() -> int:
    """Print every comment found and return a non-zero exit code if there is any."""
    findings = [
        (path, line)
        for directory in CHECKED_DIRECTORIES
        for path in sorted((ROOT / directory).rglob("*.py"))
        for line in comment_lines(path)
    ]
    for path, line in findings:
        print(f"{path.relative_to(ROOT).as_posix()}:{line}: comment")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
