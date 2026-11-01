"""One video file streamed through one live session, start to end (docs/features/live.md)."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from .._core import ApiClient, FenonError, LiveSessionError, NetworkError, Runtime
from ._generated import LiveSession
from .config import LIVE_LIMITS
from .encoder import EncodedFrame, H264Encoder
from .gateway import GatewayClient
from .results import LiveRun, ResultLog
from .sessions import GatewayAccess, LiveSessions, gateway_access
from .source import Pacer, VideoFile
from .transport import LiveTransport

_REPLACE_SESSION_CODES = frozenset({"already_connected", "invalid_token"})
_OWN_END_REASONS = frozenset({"client_closed", "deleted"})


def run_file(api: ApiClient, runtime: Runtime, *, model: str, path: str | os.PathLike[str]) -> LiveRun:
    with VideoFile(path) as video:
        sessions = LiveSessions(api)
        attempts = LIVE_LIMITS.max_sessions_per_call
        for attempt in range(1, attempts + 1):
            created = sessions.create(model=model, input_duration_s=video.info.duration_s)
            access = gateway_access(created, runtime.now())
            if access is None:
                sessions.end(created.id)
                continue
            stream = _Stream(access, video, runtime)
            failure: FenonError | None = None
            try:
                _run_to_completion(stream.run)
            except FenonError as error:
                failure = error
            finally:
                ended = sessions.end(created.id)
            replaceable = failure is not None and failure.code in _REPLACE_SESSION_CODES
            if replaceable and not stream.started_media and attempt < attempts:
                continue
            run = stream.outcome(model, ended)
            if failure is not None and not stream.started_media:
                raise failure
            if failure is not None or not stream.finished or ended.end_reason not in _OWN_END_REASONS:
                raise _ended_early(run, failure)
            return run
        raise LiveSessionError(
            "fenon-api didn't give a live session the SDK could connect to.",
            code="no_usable_session",
            fix="Try again.",
            retryable=True,
        )


class _Stream:
    def __init__(self, access: GatewayAccess, video: VideoFile, runtime: Runtime) -> None:
        self._access = access
        self._video = video
        self._runtime = runtime
        self.log = ResultLog()
        self.started_media = False
        self.finished = False

    async def run(self) -> None:
        gateway = GatewayClient(self._access, self._runtime)
        transport = LiveTransport(self._access.ice_servers, self.log.record_message)
        try:
            answer = await asyncio.to_thread(gateway.offer, await transport.create_offer())
            await transport.accept_answer(answer)
            await transport.wait_connected()
            self.started_media = True
            await self._send_video(transport)
            await asyncio.sleep(LIVE_LIMITS.end_of_input_wait_s)
            self.finished = not transport.lost.is_set()
        finally:
            await asyncio.to_thread(gateway.hang_up)
            await transport.close()
            gateway.close()

    def outcome(self, model: str, session: LiveSession) -> LiveRun:
        frames, gaps, unmatched = self.log.collect()
        return LiveRun(
            session_id=session.id,
            model=model,
            frames=frames,
            gaps=gaps,
            end_reason=session.end_reason,
            usage=session.usage,
            frames_sent=self.log.frames_sent,
            unmatched_results=unmatched,
        )

    async def _send_video(self, transport: LiveTransport) -> None:
        info = self._video.info
        encoder = H264Encoder(
            width=info.width, height=info.height, frame_rate=info.frame_rate, time_base=info.time_base
        )
        pacer = Pacer(asyncio.get_running_loop().time, asyncio.sleep)
        frames = self._video.frames()
        while (frame := await asyncio.to_thread(next, frames, None)) is not None:
            encoded = await asyncio.to_thread(encoder.encode, frame.picture, frame.index)
            await pacer.wait_for(frame.time_s)
            await self._send(transport, encoded)
        await self._send(transport, await asyncio.to_thread(encoder.flush))
        await transport.end_media()

    async def _send(self, transport: LiveTransport, encoded: list[EncodedFrame]) -> None:
        for packet in encoded:
            self.log.record_sent(
                packet.pts, packet.time_base, packet.frame_number, float(packet.pts * packet.time_base)
            )
            await transport.send(packet)


def _ended_early(run: LiveRun, failure: FenonError | None) -> LiveSessionError:
    if failure is not None and not isinstance(failure, NetworkError):
        error = LiveSessionError(
            f"The live session stopped before the whole video was sent: {failure.message}",
            code=failure.code,
            fix=failure.fix,
            retryable=False,
        )
    else:
        reason = run.end_reason or (failure.code if failure else None) or "connection_lost"
        error = LiveSessionError(
            f"The live session ended before the whole video was sent ({reason}).",
            code=reason,
            fix="Try again; `result` holds the results that arrived.",
            retryable=True,
        )
    error.result = run
    return error


def _run_to_completion(make: Callable[[], Coroutine[Any, Any, None]]) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(make())
        return
    loop = asyncio.new_event_loop()
    task = loop.create_task(make())
    failures: list[BaseException] = []

    def work() -> None:
        try:
            loop.run_until_complete(task)
        except BaseException as error:
            failures.append(error)
        finally:
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

    worker = threading.Thread(target=work, name="fenon-live-session", daemon=True)
    worker.start()
    try:
        worker.join()
    except KeyboardInterrupt:
        loop.call_soon_threadsafe(task.cancel)
        worker.join()
        raise
    if failures:
        raise failures[0]
