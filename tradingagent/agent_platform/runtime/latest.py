"""A single immutable frame; slow SSE readers never accumulate a history queue."""

import asyncio
from uuid import uuid4

from agent_platform.domain.overview import AccountSource, DataMode, MarketSource, OverviewFrame
from agent_platform.ports.clock import ClockPort


class LatestOverview:
    def __init__(
        self,
        clock: ClockPort,
        *,
        mode: DataMode = "disabled",
        market_source: MarketSource = "none",
        account_source: AccountSource = "none",
    ):
        self._process = uuid4().hex
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._scope = mode, market_source, account_source
        self._frame = OverviewFrame(
            event_id=f"{self._process}:0",
            mode=mode,
            market_source=market_source,
            account_source=account_source,
            captured_at=clock.utcnow(),
        )

    async def latest(self) -> OverviewFrame:
        return self._frame

    async def publish(self, frame: OverviewFrame) -> None:
        checked = OverviewFrame.model_validate_json(frame.model_dump_json())
        if (checked.mode, checked.market_source, checked.account_source) != self._scope:
            raise ValueError("overview provider scope cannot change after startup")
        async with self._condition:
            self._sequence += 1
            self._frame = checked.model_copy(
                update={"event_id": f"{self._process}:{self._sequence}"}
            )
            self._condition.notify_all()

    async def wait(self, after_id: str) -> OverviewFrame:
        async with self._condition:
            await self._condition.wait_for(lambda: self._frame.event_id != after_id)
            return self._frame
