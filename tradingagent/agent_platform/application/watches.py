"""Lane-bound orchestration over pure rules and atomic storage."""

import asyncio
from datetime import datetime

from agent_platform.domain.common import required_identifier
from agent_platform.domain.watch_rules import evaluate_watch
from agent_platform.domain.watches import WatchFrame, WatchRecord
from agent_platform.ports.sessions import RevisionConflict
from agent_platform.ports.watches import WatchStorePort


class WatchService:
    def __init__(self, store: WatchStorePort, *, lane_id: str):
        self.store, self.lane_id = store, required_identifier(lane_id)
        self._lock = asyncio.Lock()

    async def process(self, frame: WatchFrame, now: datetime) -> tuple[WatchRecord, ...]:
        async with self._lock:
            results = []
            for watch in await self.store.list_monitored(self.lane_id, now):
                if (watch.definition.symbol, watch.definition.interval) != (
                    frame.symbol,
                    frame.interval,
                ):
                    continue
                try:
                    results.append(
                        await self.store.commit_evaluation(
                            watch.definition.watch_id,
                            watch.revision,
                            frame,
                            evaluate_watch(watch, frame, now),
                        )
                    )
                except RevisionConflict:
                    # A cancellation/replacement wins. The next frame uses the new version.
                    continue
            return tuple(results)

    async def expire(self, now: datetime) -> tuple[WatchRecord, ...]:
        async with self._lock:
            return await self.store.expire(self.lane_id, now)

    async def active(self) -> tuple[WatchRecord, ...]:
        return await self.store.list_active(self.lane_id)

    async def monitored(self, now: datetime) -> tuple[WatchRecord, ...]:
        return await self.store.list_monitored(self.lane_id, now)
