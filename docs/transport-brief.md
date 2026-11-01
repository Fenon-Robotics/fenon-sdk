# Live transport brief for the Python SDK

How a client streams video to the Fenon live gateway over WebRTC and reads results back, as of 2026-10-31. This
brief covers only the gateway leg. For the session API, its errors and usage, see
[sdk-note.md §3](sdk-note.md#3-session-api-calls-fenon-api).

**What is proven.** A werift (TypeScript) test client streamed a re-encoded real clip through the gateway, in process
and over the network from India to us-east-1: 272 of 272 frames came back bit for bit, p50 207 ms, no gaps
(`docs/live.md:206-214`). **aiortc has never been run against the gateway.** The aiortc facts below come from reading
its public 1.15.0 source on GitHub (the current PyPI release). That code was read, not run.

**Citations.** `oas:` = `fenon-api/contracts/openapi.yaml`, `gw/` = `services/live-gateway/`, `support/` =
`tests/unit/support/`, `e2e/` = `tests/live-e2e/`, and bare paths start at the fenon-app root. `aiortc:` = `src/aiortc/`
at 1.15.0, `werift:` = fenon-app `node_modules/werift/lib/webrtc/src/` (0.25.2), `sdk:` = this repo. fenon-app line
numbers are from the uncommitted `feat/backend` working tree on 2026-10-31, so they may move.

## 1. Flow

| # | Step | Evidence |
|---|---|---|
| 1 | Create the session: `POST /v1/live/sessions` with the API key, an `Idempotency-Key` and `{model, max_result_rate?, max_duration_s?}`. The proven run sent `{"model": "fenon/hand-pose-hm", "max_result_rate": 30}`. The 201 response carries `connection` | oas:109-136, 875-892, 943-961; e2e/remote-gateway.test.ts:73-79 |
| 2 | Before calling `createOffer`, add a `sendonly` video transceiver that offers only H.264 (`level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f`, 90 kHz, NACK and PLI). Then create the `results` data channel with `{ordered: false, maxRetransmits: 1}`. Pass `connection.ice_servers` as given | support/loopback-client.ts:60-77; e2e/remote-gateway.test.ts:125 |
| 3 | There is no trickle ICE. Set the local description and wait until ICE gathering is `complete` | support/loopback-client.ts:97-101; oas:265-266 |
| 4 | `POST connection.offer_url` with `Authorization: Bearer <connection.token>` and `Content-Type: application/sdp`, before `token_connect_by` (60 s after creation). The gateway compares whole Unix seconds, so leave a margin | support/loopback-client.ts:45-51, 103-108; oas:262, 948; gw/http/signaling-server.ts:68 |
| 5 | On 201, apply the body as the SDP answer. It already contains all of the gateway's candidates | support/loopback-client.ts:106; gw/peer/peer-connection.ts:139-147 |
| 6 | Start streaming only once the peer is `connected` and the channel is `open`. If the channel isn't open 5 s after media connects, the session ends `failed` | e2e/remote-gateway.test.ts:129; e2e/real-video.test.ts:39; gw/session/live-session-runner.ts:42, 239 |
| 7 | Stream (§3) and read results (§4) | |
| 8 | End: `DELETE connection.session_url` with the token, or `DELETE /v1/live/sessions/{id}` with the API key. Then close the peer (§5) | e2e/remote-gateway.test.ts:185, 233; support/loopback-client.ts:119-121 |

## 2. Offer rules and the gateway's answers

The gateway answers `422 offer_invalid`, with a message naming the rule, unless the offer has all of these
(gw/peer/sdp-policy.ts:25-49; oas:262-264):

- exactly one `m=video` section, neither `recvonly` nor `inactive`;
- no `a=simulcast` and no `a=rid`;
- at least one `H264/90000` payload type with `packetization-mode=1` and a `profile-level-id` whose profile_idc is
  `42` and whose constraint byte is `00` or has `0x40` set (`42e0xx`, `4200xx`);
- an `m=application … webrtc-datachannel` section.

Audio is ignored. The label is checked only when the channel opens. A channel with another label is ignored and the
session ends `failed` (gw/peer/peer-connection.ts:132-137; tests/unit/gateway-loopback.test.ts:169-173).

The answer is H.264 `42e01f` or `42001f` with NACK, PLI and transport-cc, and nothing else: no RTX, no audio, and no
header extension other than transport-cc (gw/peer/peer-connection.ts:57-69, 100-107).

The checks run in the order of the rows below. An error is `retryable` only when the gateway sends `Retry-After`
(gw/http/signaling-server.ts:59-104, 132-135; gw/http/responses.ts:32-48; oas:269-336).

| Status `code` | Retry | Action |
|---|---|---|
| 503 `unavailable` (draining) | after 5 s | retry before `token_connect_by` |
| 415 `unsupported_media_type`, 413 `request_too_large` (> 64 KB) | no | client bug |
| 401 `invalid_token` | no | wrong token, past `token_connect_by`, or the session has ended or no longer exists: create a new session |
| 409 `already_connected` | no | the session already has a connection, on this or any other gateway (fenon-api records one per session, Fenon F31): create a new session |
| 501 `not_implemented` | no | no model engine on this gateway |
| 422 `offer_invalid` | no | fix the offer; the same session still accepts another one before `token_connect_by` (oas:272) |
| 503 `engine_unavailable` | after `Retry-After` (5 s or the engine's) | the engine refused, failed or took > 5 s |
| 503 `unavailable` (no media port, or fenon-api couldn't record the connection) | after 5 s | retry the same offer; nothing was claimed |
| 500 `internal_error` | after 1 s | |

## 3. Video

| Topic | What the gateway does | What our client does |
|---|---|---|
| Profile | Checks every new SPS, whatever the SDP said. profile_idc must be 66 (Baseline or Constrained Baseline); any other value ends the session `unsupported_stream`. An unreadable SPS drops the frame and triggers a keyframe request (gw/media/video-pipeline.ts:128-137; gw/media/h264.ts:8; tests/unit/gateway-adversarial.test.ts:347-357) | Encodes with libx264 `-profile:v baseline -level 3.1 -pix_fmt yuv420p`; the gateway receives `avc1.42C01F` (e2e/annexb-clip.ts:33-37; e2e/real-video.test.ts:69) |
| RTP | RFC 6184 mode 1 only (single NAL, STAP-A, FU-A); any other packet type makes the frame invalid. A frame ends at the marker bit or a timestamp change. A frame over 2048 packets is dropped with `gap: frame_too_large` (gw/media/h264.ts:23-49; gw/media/frame-assembler.ts:1-10, 21, 72-81) | One timestamp per frame, marker on its last packet. A NAL goes whole if ≤ 1200 bytes, else as FU-A; no STAP-A (support/h264-rtp.ts:37-80; support/loopback-client.ts:110-117) |
| Keyframes | Forwards nothing until an IDR whose SPS and PPS it knows; it drops the frames before it and asks for a keyframe. After a loss, an incomplete frame, a busy engine or an engine request, it sends one `gap`, drops frames until the next IDR, and sends PLI at most every 500 ms (gw/media/keyframe-gate.ts:1-28; gw/media/video-pipeline.ts:1-12, 94-126; gw/media/parameter-sets.ts:63-71) | Starts on an IDR. Puts a keyframe every K = round(fps × 2) frames (`-g K -keyint_min K -sc_threshold 0`), with no B-frames (`-bf 0`) (e2e/annexb-clip.ts:34-36; e2e/real-video.test.ts:22, 34). **It never answers PLI** (support/loopback-client.ts reads no RTCP), so recovery waits for the next periodic keyframe, which the contract now states (oas, `offerLiveSession`); answering PLI is unproven with any client |
| SPS/PPS | Cached by ID and added to IDRs that lack them, but only after it has received them (gw/media/parameter-sets.ts:1-7, 77-83) | `repeat-headers=1`: SPS and PPS before every IDR (e2e/annexb-clip.ts:36) |
| Slices | Any number | 1 and 4 per frame proven; `-tune zerolatency` added when > 1 (e2e/annexb-clip.ts:32-36; e2e/real-video.test.ts:57-62) |
| Rotation | Not negotiated (no orientation extension) and described as 0: the sender rotates the pixels (oas:265; gw/media/stream-description.ts:35; gw/peer/peer-connection.ts:103) | No rotation step (e2e/annexb-clip.ts:33-37); a rotated clip is untested |
| Size change | Accepted at an IDR with a new SPS; the stream is described again (gw/media/video-pipeline.ts:128-140; unit test tests/unit/gateway-media.test.ts:304-310). Each result's `frame_width`/`frame_height` give the displayed (cropped) size that `data` refers to (oas:1006-1007) | Untested end to end |

**Sending a file the way the proven client does it:**

1. Re-encode with `ffmpeg -i SRC -an -c:v libx264 -profile:v baseline -level 3.1 -pix_fmt yuv420p -g K -keyint_min K
   -sc_threshold 0 -bf 0 -x264-params repeat-headers=1:slices=N -f h264 OUT`, adding `-tune zerolatency` when N > 1.
   Keep the original size (e2e/annexb-clip.ts:31-39).
2. Take `fps` from ffprobe's `avg_frame_rate` (e2e/annexb-clip.ts:45-49).
3. Split the stream at its 3- or 4-byte start codes and group the NAL units into frames per H.264 §7.4.1.2.3
   (e2e/annexb-clip.ts:51-90).
4. Send frame i with RTP timestamp `90000 + i × round(90000 / fps)`, then sleep until `start + (i + 1) / fps` s
   (e2e/real-video.test.ts:41-46; e2e/remote-gateway.test.ts:145-157). The timestamps come from the average frame rate,
   not each frame's PTS, so variable-frame-rate files aren't handled.

Result: every frame reached the engine byte-identical, decoded to exactly the clip's pictures, with exact media times
and no discontinuity (e2e/real-video.test.ts:61-78).

## 4. Results channel

- **The client creates it:** label `results`, options `{ordered: false, maxRetransmits: 1}` (oas:87-88). The gateway
  matches only the label. werift's partial reliability doesn't work as specified, so the gateway enforces its own rules
  (gw/results/results-gate.ts:1-9).
- **Messages** are JSON text: `result`, `gap`, `end` (oas:985-1032; gw/results/messages.ts:10-34).
- **Delivery is lossy** (oas:89-93; gw/results/results-gate.ts:49-52, 59-85):
  - above 32 KB buffered, only the newest result waits;
  - a result is dropped 500 ms after its frame arrived, or when it is over 16 KB;
  - `gap` and `end` are always sent while the channel is open;
  - at most `max_result_rate` results per second are sent.
- **`frame_id`** counts every complete frame, so missing IDs are frames that got no result
  (gw/media/video-pipeline.ts:4, 115).
- **`gap`** reports an abnormal stall (`packet_loss`, `keyframe_wait`, `engine_restart`, `frame_too_large`). Results
  resume at the next keyframe (oas:1013-1023).
- **`data`** for `fenon/hand-pose-hm` is a **draft**
  (`fenon-api/contracts/models/fenon.hand_pose.live/0.1.0/data.schema.json`; oas:1188-1191). Don't depend on it before
  1.0.0.

**Pairing results with frames.** `rtp_timestamp` is the frame's RTP timestamp as sent (oas:1004;
gw/session/live-session-runner.ts:184). Our client picks each timestamp itself and looks results up by it
(e2e/remote-gateway.test.ts:150-164). That works because werift's `writeRtp` keeps the caller's timestamps: the
offset starts at 0 and changes only on a source switch (werift:media/rtpSender.js:323-334, 752-778).

**aiortc is different.** Its RTP timestamp is a `random32()` origin plus the frame's pts at 90 kHz. The origin is a
local variable in `_run_rtp`, and `getStats()` doesn't report it (aiortc:rtcrtpsender.py:167-191, 357-375;
aiortc:codecs/h264.py:295, 301). The only copy is the private `_RTCRtpSender__rtp_timestamp`, which holds the last
packet sent (aiortc:rtcrtpsender.py:126, 404). **UNVERIFIED:** whether aiortc has a supported way to learn the origin.

Don't pair on `media_time_90k` alone. Its zero is the first complete frame the gateway assembled, which may not be
the client's first frame (gw/media/video-pipeline.ts:116-117).

**Decided (Fenon F32, not built yet):** the SDK puts its own frame number in an H.264 SEI user-data unit before each
frame, and the gateway echoes it in that frame's `result` as a new optional field. That needs the SDK to send frames
it encoded itself (the "pre-encoded frames" row in §6). The SDK's first transport task is a short test that aiortc
carries the SEI unit to the gateway; the contract field and the gateway side follow it. Results are already ordered by
`frame_id`/`media_time_90k`; this is only for matching a result to the exact source frame.

## 5. Ending and errors

| Trigger | `end_reason` | Evidence |
|---|---|---|
| `DELETE session_url` (token) | `client_closed` | gw/http/signaling-server.ts:107-114; e2e/remote-gateway.test.ts:229-239 |
| `DELETE /v1/live/sessions/{id}` (API key); the gateway stops at its next checkpoint, ≤ 5 s | `deleted` | oas:232-235; e2e/remote-gateway.test.ts:185-187 |
| No RTP for `idle_timeout_s` (15 s today) | `idle_timeout` | gw/session/live-session-runner.ts:237; oas:919 |
| `max_duration_s` of connected time (the clock pauses while ICE is disconnected) | `max_duration` | live-session-runner.ts:238; gw/session/connected-clock.ts:1-5 |
| ICE fails or closes, before or after the first connection | `never_connected` / `client_closed` | live-session-runner.ts:214-228 |
| No connection by `connect_by` | `never_connected` | live-session-runner.ts:234 |
| Channel not open 5 s after connect; the engine fails | `failed` | live-session-runner.ts:198-203, 239 |
| An SPS that isn't Baseline | `unsupported_stream` | live-session-runner.ts:101-104 |
| The gateway drains | `server_shutdown` | gw/http/signaling-server.ts:139-152 |

- **Hang-up can take long.** `200 {"status":"ended"}` comes only after the gateway has sent `end`, closed the engine
  (≤ 1 s), drained the channel (≤ 250 ms), closed the peer and sent the final usage report. That report gets up to 5
  attempts with a 5 s timeout each and 0.5–4 s of backoff between them, so use a long timeout
  (live-session-runner.ts:37-46, 273-289; gw/checkpoints/checkpoint-reporter.ts:11-36).
- **Other hang-up answers:** `404 not_found` means there is no connection on this gateway yet or any more: already
  ended, or an offer still being processed that may still connect. On a 404, also `DELETE /v1/live/sessions/{id}`;
  fenon-api then refuses that offer's connection claim, or the gateway stops it at its next checkpoint (Fenon F31).
  `401` means the token has expired (gw/http/signaling-server.ts).
- **Hanging up discards results still being processed** (the runner ignores results once ending,
  gw/session/live-session-runner.ts). For a file, wait for the last frame's result, or about 1 s without results,
  before hanging up.
- **`max_duration_s` cuts a long file off** (default 600 s, at most 3600 s of connected time); set it to cover the file.
- **`end` is best effort.** The record is `end_reason` from `GET /v1/live/sessions/{id}` (oas:1028). If the gateway
  dies, no `end` arrives, and fenon-api records `gateway_lost` after 30 s of silence (oas:70-71;
  e2e/remote-gateway.test.ts:252-255).
- **One connection per session (Fenon F31).** fenon-api records the session's one connection when the gateway accepts
  the first offer. After a dropped connection (no ICE restart), a gateway restart or any `409 already_connected`, create
  a new session; a replayed create returns `connection: null` once the session's connection is taken.
- **Hang up on every exit path.** Closing the peer without the DELETE is untested
  ([sdk-note §4](sdk-note.md#4-gateway-protocol-the-sdks-main-new-work)).

## 6. Mapping to aiortc 1.15.0

All of this was read in the source; nothing was run.

| Step | aiortc | Open points |
|---|---|---|
| ICE servers | `RTCConfiguration(iceServers=[RTCIceServer(urls, username, credential)])` (aiortc:rtcconfiguration.py:7-23). It uses only the first STUN URL and the first usable TURN URL: `turn:` needs transport udp or tcp, `turns:` needs tcp (aiortc:rtcicetransport.py:96-138) | **UNVERIFIED:** which Cloudflare URL is picked |
| Video | `pc.addTransceiver(track, direction="sendonly")` (aiortc:rtcpeerconnection.py:483-485). The default offer lists VP8, H.264 `42001f` and H.264 `42e01f` (mode 1), with NACK, PLI, goog-remb and RTX (aiortc:codecs/__init__.py:65-104). Read against the policy (gw/peer/sdp-policy.ts:29-35), it qualifies, and the gateway can only answer H.264. `setCodecPreferences` can drop VP8 (aiortc:rtcrtptransceiver.py:101-121) | **UNVERIFIED:** whether the gateway accepts it |
| Channel | `pc.createDataChannel("results", ordered=False, maxRetransmits=1)`, sent in the DCEP open (aiortc:rtcpeerconnection.py:604-634; aiortc:rtcsctptransport.py:1732-1742). The data section is `UDP/DTLS/SCTP webrtc-datachannel` (aiortc:rtcpeerconnection.py:208-209). Messages arrive as `str` on `"message"` (aiortc:rtcsctptransport.py:1816) | |
| No trickle | `setLocalDescription` gathers every candidate before it returns (aiortc:rtcpeerconnection.py:859); post `pc.localDescription.sdp` | |
| Raw frames, aiortc encoder | libx264 with `profile="Baseline"`, `level=31`, `tune=zerolatency`, yuv420p, 1 Mbps (clamped 0.5–3) (aiortc:codecs/h264.py:20-22, 269-281). The RTP time comes from `frame.pts`/`time_base` (h264.py:295). The encoder is rebuilt when the size changes (h264.py:251-260). PLI or FIR forces the next frame to picture type I (aiortc:rtcrtpsender.py:276-281, 351-355; h264.py:262-264) | **UNVERIFIED:** Constrained Baseline output (the gateway needs only profile 66); whether a forced I frame is an IDR with SPS/PPS; the periodic keyframe interval (aiortc sets none). The bitrate follows only REMB (rtcrtpsender.py:283-292), which the gateway doesn't negotiate, so it likely stays at 1 Mbps (inference) |
| Pre-encoded frames | A track whose `recv()` returns `av.Packet` (Annex B, with `pts`/`time_base`) goes to `encoder.pack()`. That splits at start codes, sends NALs over 1300 B as FU-A, groups small NALs into STAP-A and sets the marker on the last packet (aiortc:rtcrtpsender.py:321-323, 383-385; aiortc:codecs/h264.py:25, 205-246, 298-302). This is the closest match to the proven client. As in our client, PLI has no effect here: only `encode()` can force a keyframe (rtcrtpsender.py:311-323) | Untested |
| Files via `MediaPlayer` | `decode=True` paces in real time and rebases pts to 0 (aiortc:contrib/media.py:152-155, 232-256, 344): the raw-frames path. `decode=False` passes demuxed packets on unchanged (contrib/media.py:161-222). MP4 packets aren't Annex B (our code converts them with `h264_mp4toannexb`, e2e/annexb-clip.ts:42), and they keep the file's profile, often High, which ends the session `unsupported_stream` | Inference: don't use `decode=False` for MP4 |
| RTP timestamp of a sent frame | Not exposed (§4) | **UNVERIFIED** |
| Hang-up | HTTP DELETE, then `await pc.close()` (aiortc:rtcpeerconnection.py:513) | |

The first integration test must prove the points in [sdk-note §10](sdk-note.md#10-developing-and-testing-today).

## 7. Mismatches and wrong assumptions

**The contract against the code:** the four found here (the hang-up token's lifetime, the hang-up's latency, the
origin of `media_time_90k`, and what happens without PLI answers) were fixed in the contract's wording on 2026-10-31,
after this brief was written; the code didn't change. oas line numbers above may be off by a few lines since.

**sdk-note.md against the contract and the code:** the four found here (it said the contract wasn't written and cited
the removed `docs/api/live.yaml`; it put the `501` before the key check and kept the `credit_ledger` path; it said
`data` had no shape; it said every WebRTC library adds a random RTP offset) were fixed in sdk-note.md on 2026-10-31.

**This repo against the contract:**

1. **No transport dependencies.** sdk:pyproject.toml:23-28 has no WebRTC or media dependency (no aiortc, no PyAV).
2. **A recorded-run API.** sdk:docs/openapi.yaml:14-125 (`/models`, `/uploads`, `/runs`) and sdk:README.md:39-50
   ("whole video is processed", output downloaded later, job statuses) describe one with no server. In the contract a
   file goes through a live session, results are lossy rather than one per frame, and nothing is stored
   (oas:59-63, 89-93).
3. **The meaning of "live".** sdk:docs/discussions.md:33-35 (observations with sequence numbers, episode reset),
   sdk:docs/decisions.md:180 (v1 recorded-only) and sdk:docs/specs/v1/04-sdk-hand-pose.md:123 (`mode="live"` refused
   locally) contradict the same contract text.
