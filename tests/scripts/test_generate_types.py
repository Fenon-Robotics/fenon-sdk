"""Tests for the stale-file check behind ``scripts/generate_types.py --check``."""

from __future__ import annotations

from pathlib import Path

import pytest
from generate_types import CONTRACTS, stale_files

PACKAGE = CONTRACTS[0].package
FILES = [Path("_core/_generated.py"), PACKAGE / "__init__.py", PACKAGE / "row.py"]


def write_files(root: Path, files: list[Path]) -> None:
    for file in files:
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_text(f"{file.as_posix()}\n", encoding="utf-8")


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    generated, installed = tmp_path / "generated", tmp_path / "installed"
    write_files(generated, FILES)
    write_files(installed, FILES)
    return generated, installed


def test_identical_trees_are_current(trees: tuple[Path, Path]) -> None:
    assert stale_files(*trees, FILES) == []


def test_changed_and_missing_files_are_stale(trees: tuple[Path, Path]) -> None:
    generated, installed = trees
    (installed / FILES[0]).write_text("edited\n", encoding="utf-8")
    (installed / FILES[2]).unlink()
    assert stale_files(generated, installed, FILES) == [FILES[0], FILES[2]]


@pytest.mark.parametrize("extra", ["old.py", "row.pyi", "nested/model.py", "data.json"])
def test_extra_files_anywhere_in_a_contract_package_are_stale(trees: tuple[Path, Path], extra: str) -> None:
    generated, installed = trees
    write_files(installed, [PACKAGE / extra])
    assert stale_files(generated, installed, FILES) == [PACKAGE / extra]


def test_bytecode_caches_are_ignored(trees: tuple[Path, Path]) -> None:
    generated, installed = trees
    write_files(
        installed, [PACKAGE / "__pycache__" / "row.pyc", PACKAGE / "nested" / "__pycache__" / "x.pyc"]
    )
    assert stale_files(generated, installed, FILES) == []
