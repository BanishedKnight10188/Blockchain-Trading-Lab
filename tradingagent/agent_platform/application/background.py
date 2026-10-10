"""Background jobs never run inside the JEV prediction or funds gate."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from agent_platform.domain.background import BackgroundRequest, BackgroundResult, MultiScaleSnapshot
from agent_platform.domain.multiscale import BACKGROUND_WINDOWS
from agent_platform.ports.model import ModelCallFailed


class MultiScaleContext:
    def __init__(self, *, clock, feed, model, store, refresh_seconds=900, on_close=None):
        self.clock, self.feed, self.model, self.store = clock, feed, model, store
        self.refresh_seconds = refresh_seconds
        self.result = None
        self.failure = None
        self.failure_detail = None
        self._binding = None
        self._job = None
        self._next_attempt = 0.0
        self._blocked = False
        self._enabled = False
        self._closed = False
        self._on_close = on_close
        self._lock = asyncio.Lock()

    @staticmethod
    def binding(session):
        return session.session_id, session.style_revision, session.analysis_target.symbol

    async def prepare(self, session, *, enabled):
        async with self._lock:
            if self._closed:
                return
            binding = self.binding(session)
            if self._binding != binding:
                if self._job and not self._job.done():
                    self._job.cancel()
                    await asyncio.gather(self._job, return_exceptions=True)
                if self._binding and self._binding[2] != binding[2]:
                    await self.feed.aclose()
                self._binding, self.result, self.failure = binding, None, None
                self.failure_detail = None
                self._next_attempt = 0.0
                self._blocked = False
                self.result = await self.store.latest(*binding)
                expected_model = getattr(getattr(self.model, "settings", None), "model_id", None)
                if (
                    self.result is not None
                    and expected_model
                    and self.result.model_id != expected_model
                ):
                    self.result = None  # Preserve the archive; prepare the newly selected model.
            if enabled and not self._enabled:
                self._blocked, self._next_attempt = False, 0.0
            self._enabled = enabled
            await self.feed.start(binding[2])
            now = self.clock.utcnow()
            windows = tuple(self.feed.buffer.window(i, n, now) for i, n in BACKGROUND_WINDOWS)
            short = self.feed.buffer.window("3m", 20, now)
            if not all(w.complete and w.fresh for w in (*windows, short)):
                self.feed.ensure_prime()
            if (
                not enabled
                or self.model is None
                or self._blocked
                or (self._job and not self._job.done())
            ):
                return
            if self.clock.monotonic() < self._next_attempt:
                return
            if self.result and now < self.result.expires_at - timedelta(
                seconds=min(60, self.refresh_seconds // 4)
            ):
                return
            if not all(w.complete and w.fresh for w in windows):
                return
            request = BackgroundRequest(
                request_id=uuid4().hex,
                session_id=binding[0],
                style_revision=binding[1],
                symbol=binding[2],
                style_strength=session.style.strength,
                created_at=now,
                deadline=now + timedelta(seconds=60),
                windows=windows,
            )
            self._job = asyncio.create_task(
                self._refresh(request, binding), name="four-layer-background"
            )

    async def _refresh(self, request, binding):
        try:
            await self.store.prepare(request)
            result = await self.model.analyze(request)
            result = BackgroundResult.model_validate_json(result.model_dump_json())
            if result.request != request or result.expires_at <= self.clock.utcnow():
                raise ValueError("background result mismatch")
            await self.store.finish(request, result=result)
            if self._binding == binding:
                self.result, self.failure = result, None
                self.failure_detail = None
        except asyncio.CancelledError:
            await self.store.finish(request, failure="background_interrupted")
            raise
        except ModelCallFailed as error:
            self.failure = "background_model_failed"
            self.failure_detail = {
                "code": error.reason,
                "diagnostic": error.diagnostic.model_dump(mode="json")
                if error.diagnostic
                else None,
            }
            self._blocked = True
            await self.store.finish(request, failure=error.reason, diagnostic=error.diagnostic)
        except Exception:
            self.failure = "background_model_failed"
            self._blocked = True  # Pause retries after fee/provider failure; resume explicitly.
            await self.store.finish(request, failure=self.failure)
        finally:
            self._next_attempt = self.clock.monotonic() + 60

    def status(self, session):
        now = self.clock.utcnow()
        binding = self.binding(session)
        buffer = self.feed.buffer
        windows = (
            [
                buffer.window(i, n, now).evidence()
                for i, n in (*BACKGROUND_WINDOWS, ("3m", 20), ("1s", 60))
            ]
            if buffer and buffer.symbol == binding[2]
            else []
        )
        background_ready = self.result is not None and (
            (
                self.result.request.session_id,
                self.result.request.style_revision,
                self.result.request.symbol,
            )
            == binding
            and self.result.generated_at <= now < self.result.expires_at
        )
        if self.model is None:
            reason = "background_model_unconfigured"
        elif not background_ready:
            reason = self.failure or (
                "background_pending"
                if self._job and not self._job.done()
                else "background_history_pending"
            )
        elif len(windows) != 6 or not all(w["complete"] and w["fresh"] for w in windows[-2:]):
            reason = "short_history_pending"
        else:
            reason = None
        return {
            "mode": "multiscale",
            "ready": reason is None,
            "reason": reason,
            "stream_connected": self.feed.connected,
            "stream_failure": self.feed.failure,
            "windows": windows,
            "background": self.result.context_data() if background_ready else None,
            "background_refresh_seconds": self.refresh_seconds,
            "background_model_id": getattr(getattr(self.model, "settings", None), "model_id", None),
            "failure_detail": self.failure_detail,
        }

    def snapshot(self, session):
        status = self.status(session)
        if not status["ready"]:
            raise ValueError(status["reason"])
        now = self.clock.utcnow()
        return MultiScaleSnapshot(
            background=self.result,
            short=self.feed.buffer.window("3m", 20, now),
            fast=self.feed.buffer.window("1s", 60, now),
            captured_at=now,
        )

    async def aclose(self):
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._job and not self._job.done():
                self._job.cancel()
                await asyncio.gather(self._job, return_exceptions=True)
            try:
                await self.feed.aclose()
            finally:
                if self._on_close is not None:
                    await self._on_close()
