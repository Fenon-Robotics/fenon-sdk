"""Tests for pinning fenon-api's contract: the files copied, their source note and the models it names."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from generate_types import ContractPackage, model_contracts
from sync_contracts import contract_files, source_note

OPENAPI = """\
openapi: 3.1.0
info: {title: Fenon API, version: 9.8.7}
paths: {}
x-model-data-schemas:
  fenon/hand-pose-hm:
    schema: models/fenon.hand_pose.live/0.1.0/data.schema.json
x-model-contracts:
  fenon/hand-pose-hm:
    contract: fenon.hand_pose.video
    version: 1.1.0
    schemas: models/fenon.hand_pose.video/1.1.0/
  fenon/hand-pose-hm-large:
    contract: fenon.hand_pose.video
    version: 1.1.0
    schemas: models/fenon.hand_pose.video/1.1.0/
"""


def write(root: Path, relative: str, text: str = "{}\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True)


@pytest.fixture
def contracts(tmp_path: Path) -> Path:
    root = tmp_path / "fenon-app" / "fenon-api" / "contracts"
    write(root, "openapi.yaml", OPENAPI)
    write(root, "models/fenon.hand_pose.live/0.1.0/data.schema.json")
    for name in ("input", "result", "outputs", "row"):
        write(root, f"models/fenon.hand_pose.video/1.1.0/{name}.schema.json")
    return root


def test_only_contract_files_are_copied_in_a_stable_order(contracts: Path) -> None:
    write(contracts, ".DS_Store", "x")
    write(contracts, "models/notes.txt", "x")
    write(contracts, "models/__pycache__/x.pyc", "x")
    assert [path.as_posix() for path in contract_files(contracts)] == [
        "models/fenon.hand_pose.live/0.1.0/data.schema.json",
        "models/fenon.hand_pose.video/1.1.0/input.schema.json",
        "models/fenon.hand_pose.video/1.1.0/outputs.schema.json",
        "models/fenon.hand_pose.video/1.1.0/result.schema.json",
        "models/fenon.hand_pose.video/1.1.0/row.schema.json",
        "openapi.yaml",
    ]


def test_a_folder_without_openapi_yaml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"no openapi\.yaml"):
        contract_files(tmp_path)


def test_the_source_note_names_the_commit_and_hashes_every_file(contracts: Path) -> None:
    repository = contracts.parents[1]
    git(repository, "init", "-q", "-b", "main")
    git(repository, "-c", "user.email=t@example.test", "-c", "user.name=t", "add", ".")
    git(repository, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "-m", "c")
    files = contract_files(contracts)
    note = source_note(contracts, "9.8.7", files)
    assert note.startswith("# fenon-api contract 9.8.7\n")
    assert "from fenon-app `fenon-api/contracts/`, branch `main`, commit `" in note
    assert "uncommitted" not in note
    for file in files:
        digest = hashlib.sha256((contracts / file).read_bytes()).hexdigest()
        assert f"| `{file.as_posix()}` | `{digest}` |" in note


def test_the_source_note_says_when_the_contract_has_uncommitted_changes(contracts: Path) -> None:
    repository = contracts.parents[1]
    git(repository, "init", "-q", "-b", "main")
    git(repository, "-c", "user.email=t@example.test", "-c", "user.name=t", "add", ".")
    git(repository, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-q", "-m", "c")
    write(contracts, "openapi.yaml", OPENAPI + "# changed\n")
    assert ", with uncommitted changes to the contract." in source_note(contracts, "9.8.7", [])


def test_model_contracts_come_from_the_api_contract_once_each(contracts: Path) -> None:
    pytest.importorskip("yaml")
    found = model_contracts(contracts)
    assert found == [
        ContractPackage(
            "fenon.hand_pose.live", "0.1.0", contracts / "models/fenon.hand_pose.live/0.1.0", ("data",)
        ),
        ContractPackage(
            "fenon.hand_pose.video",
            "1.1.0",
            contracts / "models/fenon.hand_pose.video/1.1.0/",
            ("input", "result", "outputs", "row"),
        ),
    ]
    assert [contract.package.as_posix() for contract in found] == [
        "models/pose/hamer/_generated_live",
        "models/pose/hamer/_generated",
    ]


def test_a_model_contract_without_an_sdk_package_is_refused(contracts: Path) -> None:
    unknown = ContractPackage("fenon.unknown.video", "1.0.0", contracts, ("input",))
    with pytest.raises(SystemExit, match=r"fenon\.unknown\.video; add it to MODEL_PACKAGES"):
        _ = unknown.package
