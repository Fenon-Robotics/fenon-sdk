# Fenon Python SDK

Run Fenon's robotics models on your own data. The first model is hand-pose estimation on recorded video.

## Install

```sh
pip install fenon
```

Python 3.11 or newer.

## Authenticate

Create an API key in the Fenon console, then:

```sh
export FENON_API_KEY=fenon_...
```

or pass it directly: `Fenon(api_key="fenon_...")`.

## Hand pose on a video

```python
from fenon import Fenon

with Fenon() as client:
    result = client.pose.hand(model="fenon/hand-pose-hm", input="clip.mp4")

    for frame in result.frames:
        for hand in frame.hands:
            print(frame.frame_index, hand.handedness, hand.pixel_landmarks[0])

    print(result.usage)
    result.save("clip.jsonl")
```

- The whole video is processed. Supported files: `.mp4`, `.mov`, `.webm`.
- `result.video`, `result.camera` and `result.joint_order` describe what the model saw and assumed.
- `result.frames` and `result.save()` download the output the first time they are used, so use them while the client is open.

## Without waiting

```python
job = client.pose.hand(model="fenon/hand-pose-hm", input="clip.mp4", wait=False)
job.status        # "preparing_input", "queued", "processing_frames", "writing_results", "succeeded", ...
result = job.result()
job.cancel()
```

## Errors

Every error from the API, storage or a run is a `fenon.FenonError` with `message`, `code`, `fix`, `details`, `request_id`, `status_code`, `retryable` and `retry_after_s`. Programming mistakes raise Python's own exceptions: `TypeError` for an argument of the wrong type, `OSError` from `save()` for a path that can't be written, and `RuntimeError` when the client is already closed.

| Error | When |
|---|---|
| `AuthError` | No key, or the key was rejected |
| `InvalidInputError` | A local check failed (missing file, unsupported file type, unsupported mode) or the API rejected the input |
| `RunFailedError` | The run failed or was cancelled; carries `run_id`, `stage`, `frame`, `timestamp_s` |
| `RateLimitError`, `NetworkError`, `ServerError` | Still failing after the SDK's retries |
| `CreditsError`, `NotFoundError`, `UnsupportedModelError` | Out of credits; unknown model or run; a model this SDK version can't read |

An error raised while waiting carries `error.job`; the run keeps going and `error.job.result()` resumes waiting.

## Configuration

```python
Fenon(api_key=None, base_url=None, timeout=60.0, max_retries=2)
```

`FENON_API_KEY` and `FENON_BASE_URL` can replace the first two arguments. Set `FENON_LOG=info` or `FENON_LOG=debug` to log each API call. Logs never contain keys, request bodies, file paths or storage URLs.

`python -m fenon` prints the installed version.

Source: https://github.com/Fenon-Robotics/fenon-sdk
