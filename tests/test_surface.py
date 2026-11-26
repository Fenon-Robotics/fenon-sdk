"""Tests that the public surface is exactly the one the contract lists."""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest

import fenon
from fenon import Fenon, HandPoseJob, HandPoseResult, Usage
from fenon.models.pose import Pose

PUBLIC = {
    "AuthError",
    "CreditsError",
    "Fenon",
    "FenonError",
    "Hand",
    "HandPoseFrame",
    "HandPoseJob",
    "HandPoseLiveFrame",
    "HandPoseLiveResult",
    "HandPoseResult",
    "InvalidInputError",
    "LiveSessionError",
    "NetworkError",
    "NotFoundError",
    "RateLimitError",
    "RunFailedError",
    "ServerError",
    "SessionLimitError",
    "UnsupportedModelError",
    "Usage",
    "__version__",
}


def keyword_parameters(function: Any) -> list[tuple[str, Any]]:
    signature = inspect.signature(function)
    assert all(
        p.kind is inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values() if p.name != "self"
    )
    return [(p.name, p.default) for p in signature.parameters.values() if p.name != "self"]


def test_all_is_exactly_the_public_surface() -> None:
    assert set(fenon.__all__) == PUBLIC
    assert all(hasattr(fenon, name) for name in PUBLIC)


def test_client_parameters_are_keyword_only_with_their_defaults() -> None:
    assert keyword_parameters(Fenon.__init__) == [
        ("api_key", None),
        ("base_url", None),
        ("project", None),
        ("timeout", 60.0),
        ("max_retries", 2),
    ]


def test_hand_parameters_are_keyword_only_with_their_defaults() -> None:
    empty = inspect.Parameter.empty
    assert keyword_parameters(Pose.hand) == [
        ("model", empty),
        ("input", empty),
        ("mode", "recorded"),
        ("wait", True),
        ("video", False),
    ]


def test_hand_has_no_range_preview_or_confidence(tmp_path: Any) -> None:
    with Fenon(api_key="fenon_k") as client:
        hand: Any = client.pose.hand
        for extra in ("range", "preview", "confidence"):
            with pytest.raises(TypeError):
                hand(model="fenon/hand-pose-hm", input=str(tmp_path / "clip.mp4"), **{extra: True})


def test_result_job_and_usage_expose_only_the_listed_members() -> None:
    result_members = {f.name for f in dataclasses.fields(HandPoseResult) if not f.name.startswith("_")}
    result_members |= {name for name in vars(HandPoseResult) if not name.startswith("_")}
    assert result_members == {
        "id",
        "model",
        "model_version",
        "video",
        "joint_order",
        "camera",
        "usage",
        "outputs_expire_at",
        "frames",
        "save",
        "save_video",
    }
    assert {name for name in dir(HandPoseJob) if not name.startswith("_")} == {
        "id",
        "status",
        "result",
        "cancel",
    }
    assert set(Usage.model_fields) == {"unit", "quantity"}
