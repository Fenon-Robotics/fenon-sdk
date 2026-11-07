# Fenon Python SDK

Run Fenon's robotics models on your own data. The first model is hand-pose estimation on video.

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
    result = client.pose.hand(model="fenon/hand-pose-hm", input="clip.mp4", video=True)

    for frame in result.frames:
        for hand in frame.hands:
            print(frame.frame_index, frame.timestamp_sec, hand.handedness, hand.pixel_landmarks[0])

    result.save("clip.jsonl")
    result.save_video("clip.mp4")

print(result.usage.quantity, result.usage.unit)    # e.g. 12 second
```

- The file is uploaded, the model processes every frame, and the call returns when the run has finished. Ctrl-C cancels the run.
- `result.frames` has one entry per frame, in order. `save()` writes them as JSON Lines.
- `video=True` also asks for the video with the hands drawn on it; `save_video()` writes it (frame N is row N). Without `video=True`, `save_video()` raises `InvalidInputError` (`video_not_requested`).
- `frames`, `save()` and `save_video()` download on first use, so use them while the client is open.
- Outputs are deleted 24 hours after the run finishes (`result.outputs_expire_at`); after that they raise `NotFoundError` (`outputs_expired`). Save what you need within the day.
- `result.usage` is the seconds the run billed.

### The video

MP4 or QuickTime (`.mp4`, `.mov`); H.264, HEVC, AV1 or VP9; at most 3840×2160; with `video=True`, an even width and height. The model checks these when the run starts; a video that misses one fails with `RunFailedError` (`input_unsupported`), and `error.details["reason"]` says which.

### Don't wait

```python
job = client.pose.hand(model="fenon/hand-pose-hm", input="clip.mp4", wait=False)
job.status      # "queued", "processing_frames", "succeeded", "failed" or "canceled"
result = job.result()    # waits; or job.cancel() to stop the run
```

## Live mode: coming soon

`mode="live"` will stream a camera feed to the model in real time. It isn't available yet: asking for it raises `InvalidInputError` (`mode_not_available`) before anything is sent.

## Errors

Every error is a `fenon.FenonError` with `message`, `code`, `fix`, `details`, `request_id`, `status_code`, `retryable` and `retry_after_s`. Programming mistakes raise Python's own exceptions: `TypeError` for an argument of the wrong type, `OSError` from `save()` for a path that can't be written, and `RuntimeError` when the client is already closed.

| Error | When |
|---|---|
| `AuthError` | No key, or the key was rejected |
| `InvalidInputError` | A local check failed (missing or unreadable file, unsupported file type or mode) or the API rejected the input |
| `RunFailedError` | The run failed or was cancelled; `code` says why (`input_unsupported`, `processing_failed`, `deadline_exceeded`, `canceled`) and `retryable` whether running it again may work |
| `NotFoundError` | Unknown model, or outputs that have expired |
| `CreditsError` | Out of credits |
| `RateLimitError`, `NetworkError`, `ServerError` | Still failing after the SDK's retries |

An error raised while waiting for a run also carries `error.job`; the run keeps going, and `error.job.result()` waits again.

## Configuration

```python
Fenon(api_key=None, base_url=None, timeout=60.0, max_retries=2)
```

`FENON_API_KEY` and `FENON_BASE_URL` can replace the first two arguments. Set `FENON_LOG=info` or `FENON_LOG=debug` to log each API call. Logs never contain keys, request or response bodies, storage URLs or file paths.

`python -m fenon` prints the installed version.

Source: https://github.com/Fenon-Robotics/fenon-sdk
