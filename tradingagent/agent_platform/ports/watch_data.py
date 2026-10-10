"""Closed public Futures data independent of JEV's multiscale subscriptions."""

from collections.abc import AsyncIterator
from typing import Protocol

from agent_platform.domain.watches import WatchFrame, WatchInterval


class WatchDataPort(Protocol):
    def stream(
        self, symbol: str, interval: WatchInterval, after: str | None
    ) -> AsyncIterator[WatchFrame]: ...
    async def latest(self, symbol: str, interval: WatchInterval) -> WatchFrame: ...
