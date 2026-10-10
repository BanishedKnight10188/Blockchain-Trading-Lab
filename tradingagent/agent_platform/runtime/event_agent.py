"""Dedicated event worker. Watches alone never schedule another analysis call."""

import asyncio

from agent_platform.ports.sessions import PersistenceUnavailable


class EventAgentRuntime:
    def __init__(self, *, lane_id, runs, events, orchestrator, clock):
        self.lane_id, self.runs, self.events, self.orchestrator, self.clock = (
            lane_id,
            runs,
            events,
            orchestrator,
            clock,
        )
        self._task = None
        self._step_lock = asyncio.Lock()
        self.last_failure = None

    async def step(self):
        async with self._step_lock:
            lane = await self.runs.lane(self.lane_id)
            if not lane.enabled:
                return None
            lease = await self.events.claim(self.lane_id, self.clock.utcnow(), 120)
            if lease is None:
                return None

            async def renew():
                while True:
                    await asyncio.sleep(30)
                    await self.events.renew(lease, self.clock.utcnow(), 120)

            renewal = asyncio.create_task(renew())
            try:
                return await self.orchestrator.review(lease, lane)
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)

    async def _loop(self):
        while True:
            try:
                await self.step()
                self.last_failure = None
            except (ValueError, OSError, TimeoutError, PersistenceUnavailable):
                self.last_failure = "event_agent_unavailable"
            await asyncio.sleep(0.5)

    async def start(self):
        if self._task is None:
            await self.runs.recover(self.lane_id, self.clock.utcnow())
            await self.events.recover(self.clock.utcnow())
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
