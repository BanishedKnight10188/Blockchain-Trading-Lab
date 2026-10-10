"""Explicitly started lane monitor; owns only its tasks, never shared clients."""

import asyncio

from agent_platform.application.watches import WatchService
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.watch_data import WatchDataPort


class WatchRuntime:
    def __init__(
        self,
        service: WatchService,
        source: WatchDataPort,
        clock: ClockPort,
        *,
        poll_seconds: float = 1.0,
    ):
        if isinstance(poll_seconds, bool) or not 0.01 <= poll_seconds <= 60:
            raise ValueError("watch poll interval must be between 0.01 and 60 seconds")
        self.service, self.source, self.clock = service, source, clock
        self.poll_seconds = poll_seconds
        self._task = None
        self._streams = {}
        self.errors = {}

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def start(self):
        if not self.running:
            self._task = asyncio.create_task(self._run(), name=f"watches:{self.service.lane_id}")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _consume(self, scope, after):
        try:
            async for frame in self.source.stream(*scope, after):
                if (frame.symbol, frame.interval) != scope:
                    raise ValueError("stream crossed its subscription scope")
                await self.service.process(frame, self.clock.utcnow())
                self.errors.pop(scope, None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors[scope] = type(error).__name__

    async def _run(self):
        try:
            while True:
                try:
                    await self.service.expire(self.clock.utcnow())
                    active = await self.service.monitored(self.clock.utcnow())
                    scopes = {(w.definition.symbol, w.definition.interval) for w in active}
                    for scope, task in tuple(self._streams.items()):
                        if scope not in scopes or task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            del self._streams[scope]
                    for scope in scopes - self._streams.keys():
                        cursors = [
                            w.last_candle_key
                            for w in active
                            if (w.definition.symbol, w.definition.interval) == scope
                        ]
                        after = min(cursors) if all(cursors) else None
                        self._streams[scope] = asyncio.create_task(
                            self._consume(scope, after), name=f"watch-feed:{scope}"
                        )
                    self.errors.pop("store", None)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.errors["store"] = type(error).__name__
                await asyncio.sleep(self.poll_seconds)
        finally:
            for task in self._streams.values():
                task.cancel()
            await asyncio.gather(*self._streams.values(), return_exceptions=True)
            self._streams.clear()
