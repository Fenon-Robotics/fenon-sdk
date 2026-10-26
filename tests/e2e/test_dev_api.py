"""The SDK end to end against the dev API simulation in ``scripts/dev_api.py``, over real sockets."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from dev_api import FAKE_FRAMES, serve

from fenon import Fenon, HandPoseJob, NotFoundError, RunFailedError, ServerError
from fenon._core._generated import ModelPage

KEY = "fenon_dev_test_simulation_key_0000"
FAST = (("preparing_input", 0.05), ("queued", 0.05), ("processing_frames", 0.1), ("writing_results", 0.05))


@pytest.fixture
def base_url() -> Iterator[str]:
    server = serve(0, KEY, FAST, poll_after_ms=250)
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def video(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(bytes(range(256)) * 512)
    return path


def test_models_lists_the_catalog(base_url: str) -> None:
    response = httpx2.get(f"{base_url}/models", headers={"Authorization": f"Bearer {KEY}"})
    page = ModelPage.model_validate(response.json())
    assert [model.id for model in page.data] == ["fenon/hand-pose-hm"]
    assert page.data[0].contract.id == "fenon.hand_pose.video"
    assert httpx2.get(f"{base_url}/models").status_code == 401


def test_the_readme_call_runs_end_to_end(base_url: str, tmp_path: Path) -> None:
    with Fenon(api_key=KEY, base_url=base_url) as client:
        result = client.pose.hand(model="fenon/hand-pose-hm", input=video(tmp_path))
        frames = list(result.frames)
        saved = result.save(tmp_path / "clip.jsonl")
    assert len(frames) == FAKE_FRAMES
    assert frames[0].hands[0].handedness == "right"
    assert (result.usage.unit, result.usage.quantity) == ("frame", FAKE_FRAMES)
    assert saved.stat().st_size > 0


def test_a_job_can_be_cancelled(base_url: str, tmp_path: Path) -> None:
    with Fenon(api_key=KEY, base_url=base_url) as client:
        job = client.pose.hand(model="fenon/hand-pose-hm", input=video(tmp_path), wait=False)
        assert isinstance(job, HandPoseJob)
        job.cancel()
        assert job.status == "canceled"
        with pytest.raises(RunFailedError) as caught:
            job.result()
    assert caught.value.code == "canceled"


def test_a_failing_video_raises_run_failed(base_url: str, tmp_path: Path) -> None:
    with Fenon(api_key=KEY, base_url=base_url) as client, pytest.raises(RunFailedError) as caught:
        client.pose.hand(model="fenon/hand-pose-hm", input=video(tmp_path, "fail-clip.mp4"))
    assert (caught.value.code, caught.value.frame) == ("variable_frame_rate", 141)


def test_an_unknown_model_is_not_found_after_the_upload(base_url: str, tmp_path: Path) -> None:
    with Fenon(api_key=KEY, base_url=base_url) as client, pytest.raises(NotFoundError) as caught:
        client.pose.hand(model="fenon/no-such-model", input=video(tmp_path))
    assert caught.value.code == "model_not_found"


def test_first_cut_mode_answers_501_except_models(base_url: str, tmp_path: Path) -> None:
    server = serve(0, KEY, FAST, poll_after_ms=250, first_cut=True)
    first_cut = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        headers = {"Authorization": f"Bearer {KEY}"}
        assert httpx2.get(f"{first_cut}/models", headers=headers).status_code == 200
        missing = httpx2.get(f"{first_cut}/runs/run_000000000000", headers=headers).json()["error"]["details"]
        assert missing == {"port": "run_store"}
        with Fenon(api_key=KEY, base_url=first_cut) as client, pytest.raises(ServerError) as caught:
            client.pose.hand(model="fenon/hand-pose-hm", input=video(tmp_path))
        assert (caught.value.code, caught.value.status_code) == ("not_implemented", 501)
        assert caught.value.details == {"port": "idempotency_store"}
        with Fenon(api_key=KEY, base_url=base_url) as client:
            assert len(list(client.pose.hand(model="fenon/hand-pose-hm", input=video(tmp_path)).frames)) == 90
    finally:
        server.shutdown()
        server.server_close()
