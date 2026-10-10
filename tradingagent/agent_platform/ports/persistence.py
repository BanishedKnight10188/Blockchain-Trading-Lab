"""Atomic journal, state, observations and budget operations with owned contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from agent_platform.domain.account import AccountSnapshot, ObservedTrade, TradeBatch, TradeCursor
from agent_platform.domain.common import MarketType
from agent_platform.domain.costs import BudgetBalance, BudgetRequest, BudgetReservation, ModelUsage
from agent_platform.domain.events import (
    AppendReceipt,
    EventPage,
    ImportResult,
    JournalEvent,
    StateRecord,
)


class EventIdentityConflict(ValueError):
    """An event identifier already refers to a different immutable fact."""


class ObservationConflict(ValueError):
    """An observed exchange identifier cannot silently replace its original fact."""


class BudgetExceeded(ValueError):
    """Existing spend and reservations leave insufficient daily budget."""


class HourlyCallLimitExceeded(ValueError):
    """The persisted rolling hourly model-call allowance is exhausted."""


class BudgetFrozen(ValueError):
    """Unresolved pricing or a recorded overrun blocks further paid calls."""


class RequestIdentityConflict(ValueError):
    """A request identifier cannot be reused with different reservation inputs."""


class DispatchAlreadyReserved(ValueError):
    """An existing reservation is evidence of prior work, never new dispatch permission."""


class ReservationNotFound(LookupError):
    """Settlement references an unknown reservation."""


class SettlementConflict(ValueError):
    """Repeated settlement cannot replace an already confirmed bill."""


@runtime_checkable
class EventStorePort(Protocol):
    async def append(self, event: JournalEvent) -> AppendReceipt: ...

    async def scan(self, after_sequence: int, limit: int) -> EventPage: ...


@runtime_checkable
class StateStorePort(Protocol):
    async def load(self, key: str) -> StateRecord | None: ...

    async def save(
        self, record: StateRecord, expected_revision: int, event: JournalEvent
    ) -> StateRecord: ...


@runtime_checkable
class ObservationStorePort(Protocol):
    async def ingest(
        self, batch: TradeBatch, account: AccountSnapshot, event: JournalEvent
    ) -> ImportResult: ...

    async def account_snapshot(
        self, account_ref: str, market_type: MarketType = MarketType.SPOT
    ) -> AccountSnapshot | None: ...

    async def cursor(
        self, account_ref: str, symbol: str, market_type: MarketType = MarketType.SPOT
    ) -> TradeCursor: ...

    async def observed_trades(
        self, account_ref: str, symbol: str, market_type: MarketType = MarketType.SPOT
    ) -> tuple[ObservedTrade, ...]: ...


@runtime_checkable
class BudgetStorePort(Protocol):
    async def reserve(
        self, request: BudgetRequest, *, require_new: bool = False
    ) -> BudgetReservation: ...

    async def settle(self, reservation_id: str, usage: ModelUsage) -> BudgetBalance: ...


class BudgetReadPort(Protocol):
    async def budget_balance(
        self, at: datetime, *, daily_limit_usd: Decimal, cumulative: bool = False
    ) -> BudgetBalance: ...
