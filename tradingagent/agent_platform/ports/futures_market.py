"""A dedicated public facts lane, independent of historical analysis and models."""

from datetime import datetime
from typing import Protocol

from agent_platform.domain.futures_market import (
    FuturesContractRules,
    FuturesFundingWindow,
    FuturesMarketSnapshot,
)


class FuturesMarketUnavailable(RuntimeError):
    pass


class FuturesMarketPort(Protocol):
    async def snapshot(self, symbol: str) -> FuturesMarketSnapshot: ...

    async def rules(self, symbol: str) -> FuturesContractRules: ...

    async def settlements(
        self, symbol: str, *, after: datetime, through: datetime | None = None
    ) -> FuturesFundingWindow: ...
