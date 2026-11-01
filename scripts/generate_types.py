"""Regenerate the SDK's typed models from the pinned fenon-api contract (docs/features/contract-sync.md)."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_CONTRACTS = ROOT / "docs" / "contracts" / "fenon-api"
PACKAGE = ROOT / "src" / "fenon"
PYPROJECT = ROOT / "pyproject.toml"
MODEL_PACKAGES: Mapping[str, Path] = {
    "fenon.hand_pose.video": Path("models/pose/hamer/_generated"),
    "fenon.hand_pose.live": Path("models/pose/hamer/_generated_live"),
}
RECORDED_SCHEMAS = ("input", "result", "outputs", "row")
CODEGEN_OPTIONS = (
    "--output-model-type",
    "pydantic_v2.BaseModel",
    "--target-python-version",
    "3.11",
    "--use-standard-collections",
    "--use-union-operator",
    "--field-constraints",
    "--enum-field-as-literal",
    "all",
    "--extra-fields",
    "ignore",
    "--type-mappings",
    "string+uri=string",
    "--use-type-alias",
    "--use-title-as-name",
    "--field-extra-keys-without-x-prefix",
    "x-repr",
    "--disable-timestamp",
    "--formatters",
    "builtin",
)


@dataclass(frozen=True)
class ContractPackage:
    contract_id: str
    version: str
    schema_dir: Path
    schemas: tuple[str, ...]

    @property
    def package(self) -> Path:
        package = MODEL_PACKAGES.get(self.contract_id)
        if package is None:
            raise SystemExit(
                f"the contract names model contract {self.contract_id}; add it to MODEL_PACKAGES"
            )
        return package

    def schema_sha256(self) -> str:
        digest = hashlib.sha256()
        for name in self.schemas:
            digest.update((self.schema_dir / f"{name}.schema.json").read_bytes())
        return digest.hexdigest()


@dataclass(frozen=True)
class OpenApiSubset:
    module: Path
    title: str
    root: str
    also: tuple[str, ...]
    secrets: dict[str, tuple[str, ...]]

    def json_schema(self, contract: Path) -> dict[str, Any]:
        components = _load_yaml(contract)["components"]["schemas"]
        selected: dict[str, Any] = {}
        pending = [self.root, *self.also]
        while pending:
            name = pending.pop()
            if name not in selected:
                selected[name] = components[name]
                pending.extend(_referenced_schemas(components[name]))
        text = json.dumps(dict(sorted(selected.items()))).replace('"#/components/schemas/', '"#/$defs/')
        definitions = json.loads(text)
        for schema, properties in self.secrets.items():
            for name in properties:
                definitions[schema]["properties"][name]["x-repr"] = False
        root = definitions.pop(self.root)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.root,
            **root,
            "$defs": definitions,
        }


API_SUBSETS = (
    OpenApiSubset(
        module=Path("_core/_generated.py"),
        title="Recorded-run shapes",
        root="Run",
        also=("UploadReservation",),
        secrets={"UploadReservation": ("upload_url",), "OutputFile": ("url",)},
    ),
    OpenApiSubset(
        module=Path("_live/_generated.py"),
        title="Live-session shapes",
        root="ResultsChannelMessage",
        also=("CreatedLiveSession", "LiveSession"),
        secrets={"Connection": ("token",), "IceServer": ("username", "credential")},
    ),
)


def pinned_contract() -> Path:
    versions = sorted(
        path for path in API_CONTRACTS.iterdir() if path.is_dir() and not path.name.startswith(".")
    )
    if len(versions) != 1:
        raise SystemExit(f"expected one pinned contract version in {API_CONTRACTS}, found {len(versions)}")
    return versions[0]


def contract_version(openapi: Path) -> str:
    return str(_load_yaml(openapi)["info"]["version"])


def model_contracts(contract: Path) -> list[ContractPackage]:
    document = _load_yaml(contract / "openapi.yaml")
    found: dict[tuple[str, str], ContractPackage] = {}
    for entry in document.get("x-model-contracts", {}).values():
        schema_dir = contract / entry["schemas"]
        found[(entry["contract"], entry["version"])] = ContractPackage(
            entry["contract"], entry["version"], schema_dir, RECORDED_SCHEMAS
        )
    for entry in document.get("x-model-data-schemas", {}).values():
        schema_file = contract / entry["schema"]
        schema_dir = schema_file.parent
        found[(schema_dir.parent.name, schema_dir.name)] = ContractPackage(
            schema_dir.parent.name,
            schema_dir.name,
            schema_dir,
            (schema_file.name.removesuffix(".schema.json"),),
        )
    return [found[key] for key in sorted(found)]


def generate(target: Path) -> list[Path]:
    pinned = pinned_contract()
    openapi = pinned / "openapi.yaml"
    source = openapi.relative_to(ROOT).as_posix()
    version = contract_version(openapi)
    files: list[Path] = []
    with tempfile.TemporaryDirectory() as directory:
        for subset in API_SUBSETS:
            schema = Path(directory) / f"{subset.root}.schema.json"
            schema.write_text(json.dumps(subset.json_schema(openapi), indent=1), encoding="utf-8")
            _run_codegen(
                schema,
                "jsonschema",
                target / subset.module,
                f"{subset.title} from {source}, generated by scripts/generate_types.py. Do not edit.",
            )
            with (target / subset.module).open("a", encoding="utf-8") as module:
                module.write(f'\n\nAPI_CONTRACT_VERSION = "{version}"\n')
            files.append(subset.module)
    for contract in model_contracts(pinned):
        names: dict[str, list[str]] = {}
        for schema in contract.schemas:
            module = contract.package / f"{schema}.py"
            _run_codegen(
                contract.schema_dir / f"{schema}.schema.json",
                "jsonschema",
                target / module,
                f"Types for contract {contract.contract_id} {contract.version} ({schema} schema), "
                "generated by scripts/generate_types.py. Do not edit.",
            )
            names[schema] = _public_names(target / module)
            files.append(module)
        init = contract.package / "__init__.py"
        (target / init).write_text(_package_init(contract, names), encoding="utf-8")
        files.append(init)
    _format([target / file for file in files])
    return files


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed files are not current")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        files = generate(target)
        if args.check:
            stale = stale_files(target, PACKAGE, files)
            for file in stale:
                print(f"out of date: src/fenon/{file.as_posix()}")
            if stale:
                print("Run: python scripts/generate_types.py")
            return 1 if stale else 0
        _install(target, files)
    return 0


def _run_codegen(source: Path, file_type: str, output: Path, header: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(source),
            "--input-file-type",
            file_type,
            "--output",
            str(output),
            "--custom-file-header",
            f'"""{header}"""',
            *CODEGEN_OPTIONS,
        ],
        check=True,
    )


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _referenced_schemas(node: object) -> list[str]:
    if isinstance(node, dict):
        names: list[str] = []
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                names.append(value.rsplit("/", 1)[1])
            else:
                names.extend(_referenced_schemas(value))
        return names
    if isinstance(node, list):
        return [name for item in node for name in _referenced_schemas(item)]
    return []


def _format(paths: list[Path]) -> None:
    subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--quiet", "--config", str(PYPROJECT), *map(str, paths)],
        check=True,
    )


def _public_names(module: Path) -> list[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
    return [name for name in names if not name.startswith("_")]


def _package_init(contract: ContractPackage, names: dict[str, list[str]]) -> str:
    seen: dict[str, str] = {}
    for schema, schema_names in names.items():
        for name in schema_names:
            if name in seen:
                raise SystemExit(f"{name} is defined by both the {seen[name]} and {schema} schemas")
            seen[name] = schema
    imports = "".join(
        f"from .{schema} import {', '.join(sorted(schema_names))}\n" for schema, schema_names in names.items()
    )
    exported = sorted([*seen, "CONTRACT_ID", "CONTRACT_VERSION", "SCHEMA_SHA256"])
    export_lines = "".join(f'    "{name}",\n' for name in exported)
    return (
        f'"""Types and constants for contract {contract.contract_id} {contract.version}, generated by '
        'scripts/generate_types.py. Do not edit."""\n\n'
        f"{imports}\n"
        f'CONTRACT_ID = "{contract.contract_id}"\n'
        f'CONTRACT_VERSION = "{contract.version}"\n'
        f'SCHEMA_SHA256 = "{contract.schema_sha256()}"\n\n'
        f"__all__ = [\n{export_lines}]\n"
    )


def stale_files(generated: Path, installed: Path, files: list[Path]) -> list[Path]:
    stale = [file for file in files if not _same(generated / file, installed / file)]
    expected = set(files)
    for model_package in MODEL_PACKAGES.values():
        package = installed / model_package
        if package.is_dir():
            stale.extend(
                path.relative_to(installed)
                for path in sorted(package.rglob("*"))
                if path.is_file()
                and "__pycache__" not in path.relative_to(package).parts
                and path.relative_to(installed) not in expected
            )
    return stale


def _same(generated: Path, installed: Path) -> bool:
    return installed.is_file() and generated.read_bytes() == installed.read_bytes()


def _install(target: Path, files: list[Path]) -> None:
    for model_package in MODEL_PACKAGES.values():
        shutil.rmtree(PACKAGE / model_package, ignore_errors=True)
    for file in files:
        destination = PACKAGE / file
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target / file, destination)


if __name__ == "__main__":
    raise SystemExit(main())
