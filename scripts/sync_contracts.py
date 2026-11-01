"""Pin a fenon-api contract version and regenerate the types (docs/features/contract-sync.md)."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import generate_types

CONTRACT_SUFFIXES = (".yaml", ".json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fenon_api", type=Path, help="path to the fenon-api folder (holds contracts/)")
    args = parser.parse_args(argv)
    source = args.fenon_api.resolve() / "contracts"
    files = contract_files(source)
    version = generate_types.contract_version(source / "openapi.yaml")
    staging = generate_types.API_CONTRACTS / f".{version}.staging"
    shutil.rmtree(staging, ignore_errors=True)
    for file in files:
        destination = staging / file
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / file, destination)
    (staging / "SOURCE.md").write_text(source_note(source, version, files), encoding="utf-8")
    for pinned in generate_types.API_CONTRACTS.iterdir():
        if pinned.is_dir() and pinned != staging:
            shutil.rmtree(pinned)
    staging.rename(generate_types.API_CONTRACTS / version)
    print(f"pinned fenon-api contract {version} ({len(files)} files)")
    return generate_types.main([])


def contract_files(source: Path) -> list[Path]:
    if not (source / "openapi.yaml").is_file():
        raise SystemExit(f"no openapi.yaml in {source}")
    return sorted(
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() and path.suffix in CONTRACT_SUFFIXES
    )


def source_note(source: Path, version: str, files: list[Path]) -> str:
    top = Path(_git(source, "rev-parse", "--show-toplevel"))
    origin = f"{top.name} `{source.relative_to(top).as_posix()}/`"
    branch = _git(source, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(source, "rev-parse", "--short", "HEAD")
    uncommitted = _git(source, "status", "--porcelain", "--", ".") != ""
    state = ", with uncommitted changes to the contract" if uncommitted else ""
    rows = "".join(
        f"| `{file.as_posix()}` | `{hashlib.sha256((source / file).read_bytes()).hexdigest()}` |\n"
        for file in files
    )
    return (
        f"# fenon-api contract {version}\n\n"
        f"Copied by `scripts/sync_contracts.py` on {datetime.now(UTC).date().isoformat()} from {origin}, "
        f"branch `{branch}`, commit `{commit}`{state}. Never edit these files by hand: re-run the script "
        "to pin another version.\n\n"
        "| File | SHA-256 |\n|---|---|\n"
        f"{rows}"
    )


def _git(directory: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
