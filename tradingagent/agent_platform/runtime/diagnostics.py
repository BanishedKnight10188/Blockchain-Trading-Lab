"""Owned, minute-frequency local diagnostic log; storage errors remain visible."""

import asyncio

from agent_platform.domain.operations import OperationalEvent
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.operations import OperationalLogPort


class DiagnosticsRuntime:
    def __init__(self, log: OperationalLogPort, clock: ClockPort, health):
        self.log, self.clock, self.health = log, clock, health
        self._lifecycle = asyncio.Lock()
        self._task = None
        self.last_failure = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    @property
    def worker_count(self):
        return int(self.running)

    async def _write(self, event):
        await self.log.write(
            OperationalEvent(
                event=event,
                occurred_at=self.clock.utcnow(),
                health=await self.health(),
            )
        )

    async def start(self):
        async with self._lifecycle:
            if self.running:
                raise RuntimeError("diagnostic worker already started")
            self.last_failure = None
            try:
                await self._write("runtime_started")
            except Exception:
                self.last_failure = "persistence"
                return
            self._task = asyncio.create_task(self._run(), name="btc-local-diagnostics")

    async def stop(self):
        async with self._lifecycle:
            if self._task is None:
                return
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            if self.last_failure is None:
                try:
                    await self._write("runtime_stopped")
                except Exception:
                    self.last_failure = "persistence"

    async def _run(self):
        try:
            while True:
                await asyncio.sleep(60)
                await self._write("worker_health")
        except Exception:
            self.last_failure = "persistence"
