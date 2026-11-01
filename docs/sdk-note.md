# Note for the SDK build: integrating with fenon-api (live perception)

Handoff from the fenon-api side to the Fenon Python SDK (`fenon-sdk`). Every value below was checked against the code
on 2026-10-31 (fenon-app, branch `feat/backend`) and fact-checked a second time, independently. It describes what
fenon-api and the live gateway do **today**, what the SDK must do, and what is still open.

## 0. Sources of truth (read these, don't assume)

1. **The API contract, owned by fenon-api:** `fenon-api/contracts/openapi.yaml` in the fenon-app repo (OpenAPI 3.1,
   version 0.2.0, pre-release; written 2026-10-31), with the per-model result schemas in `fenon-api/contracts/models/`
   (hand pose: a draft). It replaces this note wherever they differ, and the SDK generates its API types from a pinned
   copy of it with a hash check, the way it pins the hand-pose contract today. fenon-api's tests check every response
   against it.
2. **The transport** (the gateway leg: offer, video, results, hang-up, the aiortc mapping) is in
   [`transport-brief.md`](transport-brief.md), next to this note, built from the client code proven end to end.
3. **Behind both**, the code:
   - fenon-api code: `fenon-api/src/fenon_api/live/contract.py`, `live/limits.py`, `live/lifecycle.py`,
     `live_sessions/`, `routes/public_v1/live_sessions.py`;
   - gateway code: `services/live-gateway/http/signaling-server.ts`, `peer/sdp-policy.ts`, `results/messages.ts`,
     `results/results-gate.ts`, `session/live-session-runner.ts`.
4. **If this note and the contract disagree, the contract wins.** Don't hard-code a value from this note without
   finding it in the contract. Section 9 lists what is still open.
5. The SDK's own `docs/openapi.yaml` describes the recorded-run API (`/v1/uploads`, `/v1/runs`), which has **no
   server**. Don't build on it.

## 1. What changes for the SDK

- **Video files go through a live session.** There is no upload, run or polling API.
  `client.pose.hand(model="fenon/hand-pose-hm", input="clip.mp4")` must stream the file over WebRTC as if it were a
  camera (section 6). The recorded flow in the SDK (`_core/uploads.py`, `_core/runs.py`, the recorded parts of the
  generated types, `scripts/dev_api.py`) is parked and probably dropped: don't extend it.
- **Live means perception:** video frames in over WebRTC, per-frame results out on a data channel. The SDK contract's
  live text ("observations with sequence numbers, `session.step(observation)`, episode reset") describes robot
  policies such as GR00T. That isn't built and is a separate, later protocol; replace the text with this one.
- **Usage is shown in units, never money.** Live sessions are metered in seconds; prices, balances and invoices belong
  to the billing provider and the console.
- **One client protocol, two clients.** The console's browser live client (to be written later, by the fenon-app
  side) and the SDK implement the same protocol from the same contract (section 8).

## 2. Who does what

| Part | Owner | What it does |
|---|---|---|
| Session API | **fenon-api** | Checks the API key and its project; limits (5 active sessions per project, per-key rate limits); idempotency; session state; mints the session token and the ICE servers (STUN, plus TURN when configured); ends overdue sessions and finalizes unreported usage when they are read, when the project creates a session, and in the scheduled maintenance job |
| WebRTC server end | **Live gateway** | Checks the session token; enforces the offer policy; turns RTP into frames for the engine; sends results on the `results` channel; ends sessions on idle, max duration, hang-up, or a bad stream; measures usage and reports it to fenon-api |
| The model | **Engine (SCALA)** | Runs the model on frames, applies `max_result_rate`, and fills each result's `data`. **Not connected yet** (section 10) |
| Usage and billing | **fenon-api** (from the gateway's own measurements) | Connected seconds, rounded up per session, with a 10 s minimum for a session that connected and 0 for one that never did. Later it reports units to the billing provider and refuses work without balance |
| WebRTC client end, video input, results, outputs | **SDK** | Sections 3–7 |

The SDK **must not**:
- measure or compute usage or money;
- send the API key to the gateway (the gateway takes only the session token);
- invent behaviour the contract doesn't define.

## 3. Session API calls (fenon-api)

**Auth and base URL:** `Authorization: Bearer fenon_…` (a project API key), base `https://api.fenon.ai/v1` (not hosted
yet; locally `http://localhost:8000/v1`). The SDK's existing HTTP core already sends the key, `Accept`, a
`User-Agent`, and a fresh `Idempotency-Key` per logical POST, reused across retries. Keep that.

**Scope:** sessions belong to the **project**, not to the key. Any API key of the same project can read or end them.
Rate limits are counted per key.

**Create: `POST /v1/live/sessions`** (`Idempotency-Key` required: 1–64 of `A–Z a–z 0–9 _ -`; the SDK's UUIDs fit)

| Field | Rule |
|---|---|
| `model` | Required, e.g. `"fenon/hand-pose-hm"` (today the only live model) |
| `max_result_rate` | Integer 1–30, default 10: at most this many results per second |
| `max_duration_s` | Integer 10–3600, default 600: connected time after which the session ends |

Unknown fields, wrong types and out-of-range values are refused with 422.

**Order of checks today:**

| Step | Failure |
|---|---|
| API key | 401 |
| Live sessions enabled on this deployment | 501 |
| Rate limit | 429 |
| Body: content type, then size, then JSON | 415, 413, 400 |
| Field validation | 422 |
| `Idempotency-Key` present and valid | 400 (so a missing key is reported only after the body passes) |
| Idempotency state | conflict or in progress (409), or a replay |
| Model | 404 |
| ICE/TURN credentials | 503 |
| Active-session limit | 409 |
| — | 201 |

**201 response:** the session object (below) plus `connection`:

| `connection` field | Meaning |
|---|---|
| `transport` | `"webrtc"` |
| `offer_url` | The gateway URL to POST the SDP offer to |
| `session_url` | The gateway URL to DELETE to hang up |
| `results_channel` | `"results"`: the data channel label to create |
| `token` | The session token for the gateway. Secret: never log it |
| `token_connect_by` | The first connection must be made before this time (60 s after create; see section 4 on its precision) |
| `ice_servers` | `[{urls, username?, credential?}]`, to pass to the WebRTC library as given |

- **Retry with the same key and body:** returns 201 with `Idempotent-Replayed: true` and the same session. While it's
  still `waiting_for_media`, `connection` is re-issued: the same token (it's derived from the session's fixed claims)
  with freshly issued ICE servers, until a gateway takes the session's one connection (Fenon F31). After that,
  `connection` is `null`.
- **A create or replayed-create response's `usage`** is always `{unit: "second", quantity: 0, final: false}`. Read
  the session to see real usage.

**Read, list, end:**
- `GET /v1/live/sessions/{id}`: 200 with the session.
- `GET /v1/live/sessions?limit=&after=`: 200 with `{data: [session…], next_cursor}`, newest first. `limit` is 1–100
  (default 100), decimal digits only; `after` accepts only a `next_cursor` from a previous page.
- `DELETE /v1/live/sessions/{id}`: 200 with the session; ending an ended session returns it unchanged
  (`end_reason: "deleted"` otherwise). The gateway learns about it at its next checkpoint (every 5 s), so media can
  keep flowing for up to about 5 s. To stop at once, hang up at the gateway (section 4).

**The session object:** `id` (`ls_…`), `object: "live_session"`, `model`, `status`, `end_reason`, `max_result_rate`,
`max_duration_s`, `idle_timeout_s` (15), `usage`, `created_at`, `connected_at`, `ended_at`. Times look like
`2026-10-31T10:00:00.000Z`, and `connected_at` and `ended_at` are null until they happen.

**`status`:** `waiting_for_media` → `live` → `ended`, or straight from `waiting_for_media` to `ended`.
- `live` means the gateway reported that the media connection came up, so a `live` session always has
  `connected_at`. A session whose gateway answered the offer but hasn't connected yet stays `waiting_for_media`.

**`end_reason`:**

| Reason | Meaning |
|---|---|
| `client_closed` | Hang-up at the gateway, or the peer connection closed or failed after it was up. A `disconnected` state only pauses the connected clock |
| `deleted` | `DELETE` through the API |
| `idle_timeout` | 15 s without media |
| `max_duration` | Reached `max_duration_s` of connected time |
| `never_connected` | Never connected. The gateway records it at once when ICE fails or closes before the first connection; with no gateway involved, fenon-api records it 30 s after `token_connect_by` (90 s after create), when the session is next read or the maintenance job runs. A session past that point stops counting toward the 5-session limit even before it shows `ended` |
| `gateway_lost` | The gateway stopped reporting for over 30 s (recorded when the session is next read or the maintenance job runs) |
| `failed` | E.g. the `results` channel didn't open within 5 s of connecting, or the engine failed |
| `server_shutdown` | The gateway was shut down for a deploy |
| `unsupported_stream` | The video wasn't H.264 Baseline |

**`usage` = `{unit: "second", quantity, final}`.**
- The gateway reports usage through its checkpoints; fenon-api applies them in order and never after the final one.
- `final` turns true with the gateway's final checkpoint, or, if none arrives, 60 s after the session ended, with the
  last reported value (`{0, true}` when no gateway ever reported: no offer sent, deleted before any offer, every offer
  refused). Reads and the maintenance job apply this.
- **The SDK should still not block on `final`.** Treat usage as information; if it needs the settled number, read the
  session again a minute after it ended.

**Rate limits per key:** create 10, read (get and list) 120, and end 30 per minute. Over the limit you get 429.

**Errors.** Every error comes in the envelope `{error: {code, message, fix, retryable, retry_after_s, details,
request_id}}`, plus `X-Request-Id`.
- fenon-api sends `Retry-After` only on 429 `rate_limited`, 409 `idempotency_in_progress` and 503 `unavailable`.
- Its 500 is `retryable: true` with no `Retry-After`.
- `details` in use today:

  | Error | `details` |
  |---|---|
  | 400, missing or invalid key header | `header: "Idempotency-Key"` |
  | 400, bad query parameter | `field` (the parameter name) |
  | 422 | `field` (a JSON Pointer into the body) |
  | 404 `model_not_found` | `models` |
  | 409 `active_session_limit` | `limit` |
  | 429 | `limit` and `window_s` |
  | 501 | `part` |

| Status | `code` | What the SDK does |
|---|---|---|
| 400 | `invalid_request` | SDK bug (a missing `Idempotency-Key`, bad JSON) or a bad query parameter |
| 401 | `invalid_api_key` | `AuthError` |
| 404 | `model_not_found` | `NotFoundError`; `details.models` lists the live models |
| 404 | `not_found` | `NotFoundError` (no such session in this project) |
| 409 | `active_session_limit` | **New:** a user-actionable error ("end a session first"); not retryable. Today's SDK maps **every** 409 to `ServerError` (it retries `idempotency_in_progress` first), which must change |
| 409 | `idempotency_conflict` | SDK bug: a key reused with another body |
| 409 | `idempotency_in_progress` | Retry after `Retry-After` (the SDK does this already) |
| 413 | `request_too_large` | Body over 1 MB; SDK bug |
| 415 | `unsupported_media_type` | SDK bug |
| 422 | `input_invalid` | `InvalidInputError` |
| 429 | `rate_limited` | Retry after `Retry-After`, then `RateLimitError` |
| 500 | `internal_error` | Retry with the SDK's own backoff (no `Retry-After`), then `ServerError` |
| 501 | `not_implemented` | Live sessions aren't enabled on this deployment (`details.part: "live_sessions"`); only a valid key gets this answer. Never retry |
| 503 | `unavailable` | Relay credentials couldn't be issued; retry after `Retry-After` (5 s) |
| 402 | `insufficient_credits` | **Not emitted yet**; comes with billing. Map to `CreditsError` |

## 4. Gateway protocol (the SDK's main new work)

**Connect: `POST {connection.offer_url}`**
- Headers: `Authorization: Bearer {connection.token}` (not the API key) and `Content-Type: application/sdp`. The body
  is at most 64 KB.
- **Send it well before `token_connect_by`.** The gateway compares whole seconds (the token holds the deadline
  rounded down), so the real cut-off can be up to 1 s earlier than the time shown.
- **The offer must contain:**
  - **Exactly one video section sending to the gateway**, offering **H.264** with `packetization-mode=1`. Use
    `profile-level-id` `42e0xx` (Constrained Baseline) or `4200xx` (Baseline). The policy accepts a few more
    Baseline variants, but these two are what the gateway's answer itself offers (`42e01f`, `42001f`). No simulcast.
    Audio is allowed and ignored.
  - **A data channel created by the client with the label exactly `results`** and the options `{ordered: false,
    maxRetransmits: 1}`. The gateway checks only the label. A channel with any other label is ignored, so the
    session ends `failed` 5 s after connecting.
  - **All ICE candidates in the offer.** There is no trickle: gather candidates first, using
    `connection.ice_servers`.
- **Video pixels are rotated by the sender;** rotation isn't negotiated.
- **Order of checks:**

  | Step | Failure |
  |---|---|
  | Gateway draining | 503 |
  | Content type | 415 |
  | Size | 413 |
  | Token | 401 |
  | Session already connected | 409 |
  | Engine connected | 501 |
  | Offer policy | 422 |
  | Engine opens the session | 503 `engine_unavailable` |
  | Media port free, offer negotiates | 503 or 422 |
  | fenon-api records the session's one connection (Fenon F31) | 409 (another connection has it), 401 (ended or gone), 503 (fenon-api unreachable) |
  | — | 201 |

  So a gateway without an engine answers 501 even to an invalid offer: the offer policy can only be tested against a
  gateway that has an engine (section 10).
- **Responses** (the same envelope with `X-Request-Id`; gateway errors always have `fix: null` and `details: {}`, and
  `retryable` is true only when `Retry-After` is set):

| Status | `code` | Meaning, and what the SDK does |
|---|---|---|
| 201 | — | The SDP answer (`application/sdp`, all candidates) plus `Location`. Apply it |
| 401 | `invalid_token` | Token missing, wrong, past its connect deadline, or the session has ended or no longer exists: create a new session |
| 409 | `already_connected` | This session already has a connection, on this or any other gateway: create a new session |
| 413 | `request_too_large` | The offer is over 64 KB |
| 415 | `unsupported_media_type` | Not `application/sdp` |
| 422 | `offer_invalid` | The offer breaks the policy (the message names the rule) or couldn't be negotiated. The session stays open, so fix the offer and send it again |
| 501 | `not_implemented` | This gateway has no inference engine. That's true of the product gateway today (`bun run live:gateway`); the echo-engine test gateway never answers 501 |
| 503 | `engine_unavailable` / `unavailable` | The engine refused or timed out, the gateway has no free media port, it's draining, or fenon-api couldn't record the connection. Nothing was claimed: retry after `Retry-After` (5 s unless the engine says otherwise) |
| 500 | `internal_error` | Retry after `Retry-After` (1 s) |

**During the session**, the gateway ends it when:
- ICE fails or closes before the first connection (`never_connected`, at once);
- the `results` channel doesn't open within 5 s of connecting (`failed`);
- no media arrives for 15 s (`idle_timeout`);
- connected time reaches `max_duration_s` (`max_duration`);
- the H.264 stream isn't Baseline, whatever the offer said (`unsupported_stream`);
- the connection closes or fails after being up (`client_closed`).

**Keyframes:** after packet loss, an incomplete or oversized frame, an unreadable SPS, a busy engine or an engine
restart, the gateway drops frames until the next keyframe and sends a keyframe request (RTCP PLI, at most one per
500 ms). The SDK encodes frames itself (section 6, needed for frame matching) and doesn't answer these requests, so
it **must send a keyframe at a fixed interval**: every 2 s, as the proven client does. Recovery after a loss then takes
at most that interval; the contract allows it.

**Hang up: `DELETE {connection.session_url}`** with the token (valid until `token_connect_by` + `max_duration_s` +
30 s).
- **Hanging up discards results still being processed.** For a file, after sending the last frame wait until its
  result arrives, or until no result has come for about 1 s, before hanging up.
- **200 `{"status": "ended"}`** comes only after the whole teardown, including the final usage report to fenon-api.
  That's normally a second or two, but up to about 35 s when fenon-api is slow or unreachable (5 attempts with
  backoff, a 5 s timeout each). Give this call a generous timeout. The session ends `client_closed`.
- **404** doesn't always mean ended: the gateway answers 404 while an offer for the session is still being processed,
  and that offer may still connect. On a 404, also end the session through the API (`DELETE /v1/live/sessions/{id}`).
  That's safe: fenon-api refuses to record a connection for an ended session (Fenon F31), and a connection already
  recorded is stopped at the gateway's next checkpoint.
- **Always hang up with this DELETE** when the user's code finishes, fails or is interrupted. Closing the peer
  connection without it has no test: depending on how the WebRTC stack reports it, the session may end
  `client_closed` or only after 15 s as `idle_timeout`. A session left running is billed until it ends.

## 5. The `results` channel

| `type` | Fields |
|---|---|
| `result` | `frame_id` (increases per complete frame received; gaps are frames without a result), `rtp_timestamp` (the frame's RTP timestamp exactly as the client sent it), `media_time_90k` (90 kHz, 0 at the first complete frame the gateway received), `frame_width` and `frame_height` (the coordinate space of `data`, i.e. the displayed picture), `received_at_ms` and `sent_at_ms` (Unix ms, gateway clock), `data` |
| `gap` | `reason`: `packet_loss`, `keyframe_wait`, `engine_restart` or `frame_too_large` (a frame over 2048 packets was dropped; lower the bitrate). `after_frame_id` is the last frame before the pause, or null. Results pause until the next keyframe; this isn't an error |
| `end` | `reason` (an end reason from section 3) and `connected_ms`. Best effort: the session's `end_reason` from the API is the record |

**Delivery is lossy by design:**
- Messages are JSON text on an unordered channel.
- When the link is slow, only the newest result is kept.
- A result is dropped once more than 500 ms have passed since the gateway **received its frame**, so engine time
  counts too.
- A `result` over 16 KB is dropped silently. `gap` and `end` are always sent while the channel is open.
- The engine sends at most `max_result_rate` results per second.

**Never expect a result for every frame.**

**Pairing results with frames (Fenon F32):** the SDK puts its own frame number in an H.264 SEI user-data unit before
each frame, and the gateway echoes it in that frame's `result` as a new optional field. Not built yet: the contract
field and the gateway side follow the SDK's first transport test (that aiortc carries the SEI unit). Don't pair on
`rtp_timestamp` (aiortc adds a random origin it doesn't expose) or on `media_time_90k` (its zero is the first complete
frame the gateway assembled). Ordering needs neither: sort by `frame_id`.

**`data` is model-specific. For `fenon/hand-pose-hm` a draft exists:**
`fenon-api/contracts/models/fenon.hand_pose.live/0.1.0/data.schema.json` (`x-status: draft`, until the engine's
builders confirm it). It reuses the per-frame hand fields the SDK already parses from
`docs/contracts/fenon.hand_pose.video/1.0.0/row.schema.json`:
- `hands[]`: `handedness`, `pixel_landmarks`, `normalized_landmarks`, `landmarks_3d_camera`, `bbox_xyxy`,
  `joint_state`, `joint_confidence`, `hand_score`;
- `raw_hand_count`, `suppressed_duplicate_count`, `joint_order`;
- a `camera` block in every result, because the resolution can change mid-stream;
- frame identity and size come from the message (`frame_id`, `rtp_timestamp`, `frame_width`, `frame_height`).

Build the parser against the draft, but don't ship a dependency on it before 1.0.0.

## 6. Video input (SDK)

| Source | What the SDK does |
|---|---|
| Camera | Capture frames and send them as the video track |
| **File** (`.mp4` and others) | Decode, then send **at the file's own frame rate in real time** with timestamps from the file. A 60 s clip takes about 60 s and is billed about 60 s. Set `max_result_rate` up to `min(fps, 30)` for as many results as possible; it's still not every frame (section 5). Nothing is uploaded or stored on Fenon's side |
| Raw frames | Frames supplied by the user's code, through a custom video track |

Encoding: the SDK encodes frames itself with PyAV/libx264 (Constrained Baseline, `packetization-mode=1`, no
B-frames, SPS and PPS before every keyframe, a keyframe every 2 s, the frame number in an SEI unit) and hands the
encoded frames to aiortc; it doesn't use aiortc's built-in encoder. Settings proven with the gateway:
`tests/live-e2e/annexb-clip.ts` in fenon-app.

**Limits that matter for files and video:**
- **`max_duration_s` cuts a long file off:** the session ends `max_duration` after that much connected time (default
  600 s, at most 3600 s). Set it to cover the file; a file over an hour needs several sessions.
- **No size or bitrate limit in the contract.** The gateway drops any frame over 2048 RTP packets (`gap:
  frame_too_large`) and refuses an SPS outside H.264's limits. Proven end to end: 768×326 and 1280×720; 1920×1080 is
  accepted by the stream checks. Keep to those until limits are added to the contract.

## 7. Outputs (SDK, on the user's machine)

- **JSONL:** write the received results, one line per message or per typed frame.
- **Annotated frames or video (optional):** draw results on the client's own frames, paired by the frame number
  (Fenon F32).
- **Fenon stores no video, frames or results.** fenon-api keeps only the session record, usage numbers, idempotency
  records (24 h, no secrets) and rate-limit counters.

## 8. Same protocol as the console

The console will get its own live client in the browser (built-in WebRTC instead of aiortc), written later from the
same contract. Both clients follow the same rules:
- create the session (the console through its server);
- the offer rules from section 4;
- the `results` channel with the same label and options;
- result handling, including `gap` and `end`;
- pairing results with frames (the SDK by its in-video frame number, F32; a browser may use `rtp_timestamp`, which it can
  read);
- hanging up with DELETE.

A behaviour change goes into the contract first and then into both clients. The SDK must not add its own variations,
such as another channel label or other channel options.

## 9. Still open (don't hard-code)

- **The `data` shape** for hand-pose live results (section 5): a draft until the engine (SCALA) builders confirm it.
- **The model engine (SCALA) has no live intake yet.** Today it runs jobs on stored video: an encoded file in; out, an
  encoded video (`transform` workers) or per-frame records (`analysis` workers; HaMeR hand pose writes one record per
  frame, the row the draft `data` schema reuses), all to storage when the job runs. The contract carries per-frame JSON
  results on the `results` channel and no video back. Build the results path; don't build a video receive path. Live
  sessions are the only path, for cameras and files alike (Fenon F33); SCALA's live intake is later work, so until it
  lands every offer gets `501` from the product gateway (test against the echo-engine gateway, section 10).
- **Pairing results with frames under aiortc:** decided (Fenon F32): a frame number in an H.264 SEI unit, echoed in
  each `result`. Not built: the SDK's first transport task is a short test that aiortc carries the SEI unit; the
  contract field and the gateway side follow (transport-brief.md §4).
- **One connection per session** (Fenon F31, done 2026-10-31): after a dropped connection, a gateway restart or a
  `409 already_connected`, create a new session; a replay returns `connection: null` once the connection is taken.
- **Done in fenon-api on 2026-10-31** (no longer open): the API key is checked before 501; every TURN failure is a
  retryable 503; overdue sessions are ended and unreported usage finalized (reads and the maintenance job); the
  `credit_ledger` path is gone.
- **Error-detail field names:** fixed per error code in the contract.
- **Unknown values are settled by the contract's versioning rule:** new fields can appear at any time (ignore them);
  a new value in an existing enum (`status`, `end_reason`, an error code) only comes with a new contract version. So
  for the pinned contract version an unknown value is a contract mismatch: raise a clear error naming it.
- **Video size and bitrate limits:** none in the contract yet (section 6).
- **Not planned for now** (additive if they come, so nothing breaks): `GET /v1/models` with an immutable
  `model_version`; a `metadata` field on sessions; rate-limit headers on responses.
- **`402 insufficient_credits` on create** when billing lands.

## 10. Developing and testing today

- **No model is connected yet.** The product gateway (`bun run live:gateway`) answers every offer with 501 until the
  SCALA engine is plugged in.
- **Local stack** (from the fenon-app repo root):
  1. Postgres: `bun run db:up`.
  2. Migrations: `bun run db:migrate` (Better Auth's tables), then `cd fenon-api && uv sync && uv run alembic upgrade
     head` (fenon-api's tables). Without them every live request fails.
  3. fenon-api: `cd fenon-api && uv run uvicorn --factory fenon_api.app:create_app --port 8000`.
     - Required: `DATABASE_URL` and `CONSOLE_URL`.
     - For live: `LIVE_SESSION_SECRET`, `LIVE_GATEWAY_SECRET` and `LIVE_GATEWAY_URL`.
     - `LIVE_GATEWAY_URL` has no default; point it at the gateway, whose default port is 8790
       (`http://localhost:8790`).
     - See `fenon-api/.env.example`.
  4. A gateway with the **echo engine**, run from the repo root: `bun tests/live-e2e/gateway-with-echo-engine.ts`.
     - It needs the same `LIVE_SESSION_SECRET` and `LIVE_GATEWAY_SECRET`, and `FENON_API_URL` pointing at fenon-api.
     - The gateway's variables are in fenon-app's `.env.example`.
     - The echo engine answers every frame (it ignores `max_result_rate`) with facts about the frame it received
       (byte length, SHA-256, keyframe flag, codec). It's for testing the protocol, not the model.
- **API keys:** create one in the console (local dev), or in tests with Better Auth's test utilities, as
  fenon-app's `tests/unit/support/fenon-api-fixture.ts` does.
- **aiortc has never been tested against the gateway.** The gateway's own tests use a werift client, and aiortc is in
  neither repo. The SDK's first integration test, against the local stack with the echo engine, must prove:
  - aiortc's offer passes the policy;
  - media flows and results arrive;
  - periodic keyframes keep the stream recovering after loss;
  - the SEI frame number reaches the gateway (the precondition for F32's pairing);
  - DELETE hang-up ends the session `client_closed`.
- **No public gateway yet:** werift, the WebRTC library the gateway uses, has a known security defect that blocks
  public deployment (`docs/backlog.md` in fenon-app). Not the SDK's concern, but it explains why only local testing is
  possible.
