"""A separately owned Paper task; Flash is never on its critical path."""

import asyncio
import math


class PaperTradingRuntime:
    def __init__(self, service, *, interval_seconds=60):
        if (
            type(interval_seconds) not in (int, float)
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("paper interval must be finite and positive")
        if service.decision_source == "real_jev" and interval_seconds < 60:
            raise ValueError("real trial requires at least sixty seconds between cycles")
        self.service, self.interval_seconds = service, interval_seconds
        self._task = None
        self.last_failure = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def start(self):
        if self.running:
            raise RuntimeError("paper worker already started")
        await self.service.store.recover(self.service.clock.utcnow())
        if hasattr(self.service.model, "set_enabled"):
            self.service.model.set_enabled(False)
        self._task = asyncio.create_task(self._run(), name="btc-jev-paper")
        await asyncio.sleep(0)

    async def stop(self):
        task, self._task = self._task, None
        try:
            if hasattr(self.service.model, "set_enabled"):
                self.service.model.set_enabled(False)
            # One write transaction avoids a race between reading a revision and pausing.
            await self.service.store.recover(self.service.clock.utcnow())
        finally:
            if task is not None:
                task.cancel()
                cleanup = asyncio.ensure_future(asyncio.gather(task, return_exceptions=True))
                cancelled = False
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError

    async def _run(self):
        while True:
            try:
                await self.service.step()
                self.last_failure = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_failure = "paper_unavailable"
                account = await self.service.store.latest()
                if account is not None:
                    await self.service.pause(account.account_ref, account.revision)
            await asyncio.sleep(self.interval_seconds)
