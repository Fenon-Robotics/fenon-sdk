# Fenon Python SDK

Run Fenon's robotics models on your own data. The first model is hand-pose estimation on video, streamed live to the model.

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
        print(frame.frame_index, frame.time_s, hand.handedness, hand.pixel_landmarks[0])

print(result.end_reason, result.usage)    # usage in seconds of connected time
result.save("clip.jsonl")
```

- The video streams to the model in real time (`mode="live"`, the default), so a 30 s clip takes about 30 s; the call returns when it ends.
- Rotated phone videos are turned upright first. A video that changes size partway is refused.
- Results are lossy by design: `result.frames` holds the frames that got a result, in order; compare with `result.frames_sent`. `result.gaps` lists the stalls the server reported.
- If the session ends early (for example, the connection is lost), `LiveSessionError` is raised and `error.result` holds the frames that arrived.
- Works in Jupyter. Ctrl-C ends the session before the call returns.
- The live model is still being connected on Fenon's side; until it is, sessions are refused with `not_implemented`.

## Recorded mode: coming soon

Uploading a file and getting every frame back (`mode="recorded"`) isn't available yet: asking for it raises `InvalidInputError` with code `mode_not_available`, before anything is sent. Use live mode, the default.

## Errors

Every error is a `fenon.FenonError` with `message`, `code`, `fix`, `details`, `request_id`, `status_code`, `retryable` and `retry_after_s`. Programming mistakes raise Python's own exceptions: `TypeError` for an argument of the wrong type, `OSError` from `save()` for a path that can't be written, and `RuntimeError` when the client is already closed.

| Error | When |
|---|---|
| `AuthError` | No key, or the key was rejected |
| `InvalidInputError` | A local check failed (missing or unreadable file, unsupported video or mode) or the API rejected the input |
| `LiveSessionError` | The live session ended before the whole video was sent; `error.result` holds what arrived |
| `SessionLimitError` | Your project already runs as many live sessions as it may; end one first |
| `RateLimitError`, `NetworkError`, `ServerError` | Still failing after the SDK's retries |
| `CreditsError`, `NotFoundError` | Out of credits; unknown model |

## Configuration

```python
Fenon(api_key=None, base_url=None, timeout=60.0, max_retries=2)
```

`FENON_API_KEY` and `FENON_BASE_URL` can replace the first two arguments. Set `FENON_LOG=info` or `FENON_LOG=debug` to log each API call. Logs never contain keys, session tokens, request bodies or file paths.

`python -m fenon` prints the installed version.

Source: https://github.com/Fenon-Robotics/fenon-sdk
