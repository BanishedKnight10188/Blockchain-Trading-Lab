"""Atomic isolated virtual funds; no exchange submission capability."""

from datetime import datetime
from typing import Protocol

from agent_platform.domain.futures_paper import (
    FuturesPaperFunding,
    FuturesPaperOrder,
    FuturesPaperQuote,
    FuturesPaperRecord,
    FuturesPaperRules,
    FuturesPaperSettings,
    FuturesPaperState,
    FuturesPaperTransition,
)
from agent_platform.domain.trading_execution import TradeCommand


class FuturesPaperGuardConflict(ValueError):
    """The current session or independent trader no longer authorizes this work."""


class FuturesPaperStorePort(Protocol):
    async def initialize(self) -> None: ...
    async def create(
        self,
        session_id: str,
        settings: FuturesPaperSettings,
        rules: FuturesPaperRules,
        at: datetime,
        *,
        expected_style_revision: int,
    ) -> FuturesPaperState: ...
    async def get(self, account_ref: str) -> FuturesPaperState: ...
    async def lookup_trade(self, command: TradeCommand) -> FuturesPaperRecord | None: ...
    async def start(
        self,
        account_ref: str,
        expected_revision: int,
        at: datetime,
        *,
        expected_style_revision: int,
        expected_trader_revision: int,
    ) -> FuturesPaperState: ...
    async def pause(
        self, account_ref: str, expected_revision: int, at: datetime
    ) -> FuturesPaperState: ...
    async def execute(
        self,
        account_ref: str,
        expected_revision: int,
        order: FuturesPaperOrder,
        quote: FuturesPaperQuote,
        at: datetime,
        *,
        command_id: str,
        expected_style_revision: int,
        expected_trader_revision: int,
        execution_command: TradeCommand | None = None,
    ) -> FuturesPaperTransition: ...
    async def funding(
        self,
        account_ref: str,
        expected_revision: int,
        funding: FuturesPaperFunding,
        at: datetime,
        *,
        command_id: str,
    ) -> FuturesPaperTransition: ...
    async def mark(
        self,
        account_ref: str,
        expected_revision: int,
        quote: FuturesPaperQuote,
        at: datetime,
        *,
        command_id: str,
    ) -> FuturesPaperTransition | None: ...
    async def recover(self, at: datetime) -> None: ...
    async def recent(self, account_ref: str, limit: int = 50) -> tuple[FuturesPaperRecord, ...]: ...
