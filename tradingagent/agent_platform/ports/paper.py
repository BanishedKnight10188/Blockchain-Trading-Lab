"""Atomic virtual-wallet operations; no exchange write capability."""

from datetime import datetime
from typing import Protocol

from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.market import MarketSnapshot
from agent_platform.domain.paper import PaperFill, PaperIntent
from agent_platform.domain.paper_trading import PaperAccountState, PaperCycleState, PaperSettings


class PaperGuardConflict(ValueError):
    """Current session or trader configuration no longer permits paper execution."""


class PaperMarketPort(Protocol):
    async def sample(self) -> MarketSnapshot | None: ...


class PaperExecutionPort(Protocol):
    async def simulate(
        self, account: PaperAccountState, intent: PaperIntent, market: MarketSnapshot
    ) -> PaperFill: ...


class PaperStorePort(Protocol):
    async def create(
        self,
        session_id: str,
        settings: PaperSettings,
        at: datetime,
        *,
        decision_source: str = "offline_mock",
        market_source: str = "offline_demo",
        expected_style_revision: int | None = None,
    ) -> PaperAccountState: ...
    async def get(self, account_ref: str) -> PaperAccountState: ...
    async def latest(self) -> PaperAccountState | None: ...
    async def start(
        self,
        account_ref: str,
        expected_revision: int,
        at: datetime,
        *,
        expected_style_revision: int | None = None,
    ) -> PaperAccountState: ...
    async def pause(
        self,
        account_ref: str,
        expected_revision: int,
        at: datetime,
        *,
        expected_activation_revision: int | None = None,
    ) -> PaperAccountState: ...
    async def claim(
        self,
        account_ref: str,
        request_id: str,
        expected_revision: int,
        at: datetime,
        deadline: datetime,
    ) -> PaperCycleState: ...
    async def complete(
        self,
        account_ref: str,
        request_id: str,
        decision: str | None,
        confidence: str | None,
        fill: PaperFill | None,
        usage: ModelUsage | None,
        at: datetime,
        *,
        reason: str | None = None,
    ) -> PaperCycleState: ...
    async def recent(self, account_ref: str, limit: int = 50) -> tuple[PaperCycleState, ...]: ...
    async def recover(self, at: datetime) -> None: ...
