"""Owned read-only synchronization reports, independent of transport errors."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictBool, model_validator

from .account import AccountSnapshot, ObservedOrder, PositionView, TradeCursor
from .models import DomainModel, Identifier, LiveAccountRef, UtcDateTime


class SyncFailureReason(StrEnum):
    CREDENTIALS = "credentials"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TRANSPORT = "transport"
    INVALID_DATA = "invalid_data"


class AccountSignal(DomainModel):
    event_id: Identifier
    account_ref: LiveAccountRef
    kind: Literal["balance_changed", "execution_changed", "reconcile_required"]
    received_at: UtcDateTime


class SyncReport(DomainModel):
    account: AccountSnapshot
    position: PositionView | None = None
    orders: tuple[ObservedOrder, ...] = ()
    orders_as_of: UtcDateTime | None = None
    attempted_at: UtcDateTime
    next_attempt_at: UtcDateTime
    next_cursor: TradeCursor
    imported_count: int = Field(default=0, strict=True, ge=0)
    duplicate_count: int = Field(default=0, strict=True, ge=0)
    history_complete: StrictBool = False
    failure_reason: SyncFailureReason | None = None
    cached: StrictBool = False

    @model_validator(mode="after")
    def coherent_report(self):
        if self.account.as_of > self.attempted_at or self.next_attempt_at < self.attempted_at:
            raise ValueError("sync report time bounds are inconsistent")
        if any(
            order.account_ref != self.account.account_ref
            or order.symbol != "BTCUSDT"
            or order.market_type != self.account.market_type
            for order in self.orders
        ):
            raise ValueError("sync order scope is inconsistent")
        if self.failure_reason is not None and (self.orders or self.orders_as_of is not None):
            raise ValueError("failed sync cannot publish a fresh order set")
        return self
