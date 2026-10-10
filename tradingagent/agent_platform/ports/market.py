"""Owned market events and snapshots, independent of stream transports."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from agent_platform.domain.market import MarketEvent, MarketSnapshot


@runtime_checkable
class MarketDataPort(Protocol):
    def stream(self, symbols: tuple[str, ...]) -> AsyncIterator[MarketEvent]: ...

    async def latest(self, symbol: str) -> MarketSnapshot: ...
