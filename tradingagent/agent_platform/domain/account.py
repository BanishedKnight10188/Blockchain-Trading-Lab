"""Owned account facts without Binance SDK dependencies."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .common import MarketType, exact_add
from .models import (
    DomainModel,
    Identifier,
    LiveAccountRef,
    NonnegativeAmount,
    PositiveAmount,
    UtcDateTime,
)


class Balance(DomainModel):
    asset: Identifier
    free: NonnegativeAmount
    locked: NonnegativeAmount

    @property
    def total(self) -> Decimal:
        return exact_add(self.free, self.locked)


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class ObservedOrder(DomainModel):
    venue: Literal["binance"] = "binance"
    market_type: MarketType = MarketType.SPOT
    account_ref: LiveAccountRef
    symbol: Identifier
    order_id: Identifier
    side: OrderSide
    status: OrderStatus
    quantity: PositiveAmount
    filled_quantity: NonnegativeAmount
    price: NonnegativeAmount
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def consistent_fill(self) -> Self:
        if self.filled_quantity > self.quantity:
            raise ValueError("cumulative fill exceeds order quantity")
        if self.status == OrderStatus.NEW and self.filled_quantity != 0:
            raise ValueError("new order cannot already have a cumulative fill")
        if self.status == OrderStatus.FILLED and self.filled_quantity != self.quantity:
            raise ValueError("filled order must be completely filled")
        if (
            self.status == OrderStatus.PARTIALLY_FILLED
            and not 0 < self.filled_quantity < self.quantity
        ):
            raise ValueError("partially filled order requires a partial quantity")
        return self

    @property
    def identity(self) -> tuple[str, ...]:
        return self.venue, self.market_type, self.account_ref, self.symbol, self.order_id

    def apply_observation(self, newer: Self) -> Self:
        if newer.identity != self.identity or newer.side != self.side:
            raise ValueError("observation describes a different order")
        if newer.updated_at < self.updated_at:
            raise ValueError("older order observations require reconciliation")
        if newer.filled_quantity < self.filled_quantity:
            raise ValueError("cumulative order fill cannot decrease")
        if self.status == OrderStatus.PARTIALLY_FILLED and newer.status == OrderStatus.NEW:
            raise ValueError("partially filled order cannot return to new")
        if self.status.terminal and newer.status != self.status:
            raise ValueError("terminal order status cannot be replaced implicitly")
        return newer


class CostStatus(StrEnum):
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    KNOWN = "known"


class PositionView(DomainModel):
    symbol: Identifier
    quantity: NonnegativeAmount
    cost_status: CostStatus
    average_cost: PositiveAmount | None = None
    total_cost: NonnegativeAmount | None = None

    @model_validator(mode="after")
    def honest_cost(self) -> Self:
        if self.cost_status == CostStatus.UNKNOWN:
            if self.average_cost is not None or self.total_cost is not None:
                raise ValueError("unknown position cost must remain absent")
        elif self.cost_status == CostStatus.KNOWN and self.quantity > 0:
            if self.average_cost is None or self.total_cost is None:
                raise ValueError("known holding cost requires cost values")
        return self


class AccountSyncStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class AccountSnapshot(DomainModel):
    account_ref: LiveAccountRef
    market_type: MarketType = MarketType.SPOT
    balances: tuple[Balance, ...] = ()
    as_of: UtcDateTime
    status: AccountSyncStatus = AccountSyncStatus.FRESH
    account_revision: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def unique_assets(self) -> Self:
        if len({balance.asset for balance in self.balances}) != len(self.balances):
            raise ValueError("account snapshot contains duplicate assets")
        return self


class ObservedTrade(DomainModel):
    venue: Literal["binance"] = "binance"
    market_type: MarketType = MarketType.SPOT
    account_ref: LiveAccountRef
    symbol: Identifier
    trade_id: Identifier
    order_id: Identifier
    side: OrderSide
    price: PositiveAmount
    quantity: PositiveAmount
    fee: NonnegativeAmount
    fee_asset: Identifier
    quote_quantity: NonnegativeAmount | None = None
    executed_at: UtcDateTime
    executor: Literal["human"] = "human"


class TradeCursor(DomainModel):
    last_trade_id: Identifier | None = None
    last_executed_at: UtcDateTime | None = None


class TradeBatch(DomainModel):
    account_ref: LiveAccountRef
    symbol: Identifier
    market_type: MarketType = MarketType.SPOT
    trades: tuple[ObservedTrade, ...] = ()
    next_cursor: TradeCursor = Field(default_factory=TradeCursor)
    history_complete: StrictBool = False

    @model_validator(mode="after")
    def consistent_scope(self) -> Self:
        if any(
            trade.account_ref != self.account_ref
            or trade.symbol != self.symbol
            or trade.market_type != self.market_type
            for trade in self.trades
        ):
            raise ValueError("trade batch contains facts from another scope")
        if len({trade.trade_id for trade in self.trades}) != len(self.trades):
            raise ValueError("trade batch contains duplicate trade identifiers")
        return self
