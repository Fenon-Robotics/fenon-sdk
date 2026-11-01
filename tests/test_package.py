"""Tests for the package as a whole: version, entry point, layering and the shipped contract."""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from pydantic import ValidationError

import fenon
from fenon._core import Usage
from fenon.models.pose.hamer import _generated as hand_pose

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_the_installed_metadata() -> None:
    assert fenon.__version__ == version("fenon")


def test_module_entry_point_prints_the_version() -> None:
    result = subprocess.run([sys.executable, "-m", "fenon"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f"fenon {fenon.__version__}"


def resolved_imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    package = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
    if path.name != "__init__.py":
        package = package.rsplit(".", 1)[0]
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
            target = node.module if node.level == 0 else ".".join(filter(None, [base, node.module]))
            found.append((target or base, tuple(alias.name for alias in node.names)))
    return found


def imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for module, names in resolved_imports(path):
        found.add(module)
        found.update(f"{module}.{name}" for name in names)
    return found


def modules_under(relative: str) -> dict[str, set[str]]:
    return {
        path.relative_to(ROOT / "src").as_posix(): imported_modules(path)
        for path in sorted((ROOT / "src" / relative).rglob("*.py"))
    }


def test_core_imports_nothing_above_it() -> None:
    for module, imports in modules_under("fenon/_core").items():
        outside = {
            name for name in imports if name.startswith("fenon") and not name.startswith("fenon._core")
        }
        assert outside <= {"fenon._version", "fenon._version.__version__"}, (module, outside)


def test_live_imports_only_the_core_and_itself() -> None:
    allowed = ("fenon._core", "fenon._live", "fenon._version")
    for module, imports in modules_under("fenon/_live").items():
        outside = {name for name in imports if name.startswith("fenon") and not name.startswith(allowed)}
        assert outside == set(), (module, outside)


def test_only_the_live_media_modules_import_aiortc_or_av() -> None:
    media = {"fenon/_live/encoder.py", "fenon/_live/source.py", "fenon/_live/transport.py"}
    for module, imports in modules_under("fenon").items():
        third_party = {name for name in imports if name.split(".")[0] in ("aiortc", "av")}
        assert not third_party or module in media, (module, third_party)


def test_importing_fenon_loads_aiortc_and_av_only_on_the_first_live_call() -> None:
    script = (
        "import sys, fenon; print(sorted({'aiortc', 'av'} & {name.split('.')[0] for name in sys.modules}))"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


@pytest.mark.parametrize("package", ["fenon._core", "fenon._live"])
def test_packages_are_used_only_through_their_interface(package: str) -> None:
    folder = ROOT / "src" / Path(*package.split("."))
    inside = {path.stem for path in folder.iterdir() if path.suffix == ".py" or path.is_dir()}
    inside -= {"__init__", "__pycache__"}
    for path in sorted((ROOT / "src" / "fenon").rglob("*.py")):
        if path.is_relative_to(folder):
            continue
        for module, names in resolved_imports(path):
            reaches_inside = module.startswith(f"{package}.") or (
                module == package and bool(inside & set(names))
            )
            assert not reaches_inside, (path.relative_to(ROOT / "src").as_posix(), module, names)


def test_models_never_import_the_client() -> None:
    for module, imports in modules_under("fenon/models").items():
        assert not any(name.startswith("fenon._client") for name in imports), module


def test_the_pose_base_never_imports_an_implementation() -> None:
    imports = imported_modules(ROOT / "src" / "fenon" / "models" / "pose" / "base.py")
    assert not any(name.startswith("fenon.models.pose.hamer") for name in imports)
    assert "fenon._core.upload_file" in imports


def test_shipped_contract_hash_matches_the_schema_files() -> None:
    pinned = next(path for path in (ROOT / "docs" / "contracts" / "fenon-api").iterdir() if path.is_dir())
    schema_dir = pinned / "models" / hand_pose.CONTRACT_ID / hand_pose.CONTRACT_VERSION
    digest = hashlib.sha256()
    for name in ("input", "result", "outputs", "row"):
        digest.update((schema_dir / f"{name}.schema.json").read_bytes())
    assert digest.hexdigest() == hand_pose.SCHEMA_SHA256
    assert hand_pose.CONTRACT_ID == "fenon.hand_pose.video"


def test_usage_validates_and_ignores_unknown_fields() -> None:
    usage = Usage.model_validate({"unit": "frame", "quantity": 300, "future_field": True})
    assert (usage.unit, usage.quantity) == ("frame", 300)
    with pytest.raises(ValidationError):
        Usage.model_validate({"unit": "frame", "quantity": -1})


def test_generated_hand_pose_frame_accepts_a_runner_row() -> None:
    joint_order = (
        "wrist,thumb_cmc,thumb_mcp,thumb_ip,thumb_tip,index_mcp,index_pip,index_dip,index_tip,"
        "middle_mcp,middle_pip,middle_dip,middle_tip,ring_mcp,ring_pip,ring_dip,ring_tip,"
        "pinky_mcp,pinky_pip,pinky_dip,pinky_tip"
    )
    points = [[812.4 + i, 455.1 + i] for i in range(20)] + [[None, None]]
    row = {
        "frame_index": 0,
        "timestamp_sec": 0.0,
        "image_size": {"width": 1280, "height": 720},
        "joint_order": joint_order,
        "raw_hand_count": 1,
        "suppressed_duplicate_count": 0,
        "hands": [
            {
                "handedness": "right",
                "pixel_landmarks": points,
                "normalized_landmarks": points,
                "landmarks_3d_camera": [[0.01, 0.02, 0.6]] * 21,
                "bbox_xyxy": [None, None, None, None],
                "joint_state": ["in_frame_predicted"] * 20 + ["outside_frame_predicted"],
                "joint_confidence": [None] * 21,
                "hand_score": None,
                "track_id": "ignored-unknown-field",
            }
        ],
    }
    frame = hand_pose.HandPoseFrame.model_validate(row)
    assert frame.hands[0].handedness == "right"
    assert frame.hands[0].pixel_landmarks[20] == [None, None]
    with pytest.raises(ValidationError):
        hand_pose.HandPoseFrame.model_validate({**row, "joint_order": "wrist"})
