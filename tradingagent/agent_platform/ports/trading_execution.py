"""Backend-neutral submission, recovery and read-only account interfaces."""

from datetime import datetime
from typing import Protocol

from agent_platform.domain.futures_values import FuturesQuote
from agent_platform.domain.trading_execution import (
    ExecutionReceipt,
    ExecutionRecord,
    ExecutionScope,
    TradeCommand,
    TradingAccountSnapshot,
)


class ExecutionRejected(ValueError):
    """A backend knows that this command did not execute."""


class ExecutionBlocked(ValueError):
    """An unresolved command already occupies this account."""


class FuturesExecutionPort(Protocol):
    async def submit(
        self, command: TradeCommand, quote: FuturesQuote, at: datetime
    ) -> ExecutionReceipt: ...
    async def lookup(self, command: TradeCommand, at: datetime) -> ExecutionReceipt | None: ...


class FuturesTradingAccountPort(Protocol):
    async def account(self, scope: ExecutionScope, at: datetime) -> TradingAccountSnapshot: ...


class ExecutionJournalPort(Protocol):
    async def unresolved(self, account_ref: str) -> tuple[ExecutionRecord, ...]: ...
    async def reserve(
        self, command: TradeCommand, at: datetime
    ) -> tuple[ExecutionRecord, bool]: ...
    async def get(self, command_id: str) -> ExecutionRecord: ...
    async def save(
        self,
        receipt: ExecutionReceipt,
        expected_revision: int,
        *,
        quote: FuturesQuote | None = None,
    ) -> ExecutionRecord: ...
