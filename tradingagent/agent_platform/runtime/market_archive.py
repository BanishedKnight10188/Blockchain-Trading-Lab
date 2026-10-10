"""One owned archive task, sampled latest frame, bounded incremental maintenance."""

import asyncio

from agent_platform.application.market_archive import archive_records
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.market_archive import MarketArchivePort
from agent_platform.runtime.latest import LatestOverview


class MarketArchiveRuntime:
    def __init__(
        self,
        cache: LatestOverview,
        archive: MarketArchivePort,
        clock: ClockPort,
        *,
        prune_limit: int = 1000,
    ):
        if type(prune_limit) is not int or not 1 <= prune_limit <= 1000:
            raise ValueError("archive prune batch requires a 1–1000 integer")
        self.cache, self.archive, self.clock = cache, archive, clock
        self.prune_limit = prune_limit
        self._task = None
        self._lifecycle = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._pending = False
        self._last_sample = self._last_prune = None
        self._last_frame = None
        self._minutes = {}  # Only the last projected window, at most 120 identities.
        self.last_failure = None
        self.observations_written = self.observations_removed = 0

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    @property
    def worker_count(self):
        return int(self.running)

    @property
    def pending_count(self):
        return int(self._pending)

    async def start(self):
        async with self._lifecycle:
            if self.running:
                raise RuntimeError("archive worker already started")
            self.last_failure = None
            try:
                await self.archive.initialize()
            except Exception:
                self.last_failure = "persistence"
                return  # Auxiliary failure does not authorize stopping public reads.
            self._task = asyncio.create_task(self._run(), name="btc-market-archive")
            await asyncio.sleep(0)

    async def stop(self):
        async with self._lifecycle:
            if self._task is not None:
                if not self._task.done():
                    self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                self._task = None

    async def sample_once(self):
        async with self._write_lock:
            now = self.clock.monotonic()
            written = 0
            self._pending = True
            try:
                if self._last_sample is None or now - self._last_sample >= 1:
                    frame = await self.cache.latest()
                    if frame.captured_at > self.clock.utcnow():
                        raise ValueError("archive cannot consume future frames")
                    if frame.event_id != self._last_frame:
                        values = archive_records(frame)
                        minutes = {
                            value.record_id: value.source_hash
                            for value in values
                            if value.kind == "minute"
                        }
                        if any(
                            identity in self._minutes and self._minutes[identity] != digest
                            for identity, digest in minutes.items()
                        ):
                            raise ValueError("completed archive minute has changed")
                        values = tuple(
                            value
                            for value in values
                            if value.kind == "raw" or value.record_id not in self._minutes
                        )
                        if values:
                            result = await self.archive.append_many(values)
                            written = sum(result)
                            self.observations_written += written
                        self._minutes = minutes
                        self._last_frame = frame.event_id
                    self._last_sample = now
                if self._last_prune is None or now - self._last_prune >= 60:
                    result = await self.archive.prune(self.clock.utcnow(), limit=self.prune_limit)
                    self.observations_removed += result.raw_removed + result.minute_removed
                    self._last_prune = self.clock.monotonic()
                return written
            finally:
                self._pending = False

    async def _run(self):
        try:
            while True:
                await self.sample_once()
                await asyncio.sleep(1)
        except ValueError:
            self.last_failure = "invalid_data"
        except Exception:
            self.last_failure = "persistence"
