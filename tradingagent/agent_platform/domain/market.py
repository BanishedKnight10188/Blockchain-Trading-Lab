"""Closed market observations with exact prices and known event times."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from .common import MarketType
from .models import (
    DomainModel,
    FiniteDecimal,
    Identifier,
    NonnegativeAmount,
    PositiveAmount,
    UtcDateTime,
)


class Instrument(DomainModel):
    venue: Literal["binance"] = "binance"
    market_type: MarketType = MarketType.SPOT
    symbol: Identifier
    base_asset: Identifier
    quote_asset: Identifier
    filter_version: Identifier
    price_tick: PositiveAmount
    quantity_step: PositiveAmount
    min_quantity: NonnegativeAmount
    max_quantity: PositiveAmount | None = None
    min_notional: NonnegativeAmount

    @model_validator(mode="after")
    def consistent_filters(self) -> Self:
        if self.base_asset == self.quote_asset:
            raise ValueError("instrument base and quote must differ")
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("quantity filter maximum is below minimum")
        return self


class Candle(DomainModel):
    kind: Literal["candle"] = "candle"
    symbol: Identifier
    opened_at: UtcDateTime
    closed_at: UtcDateTime
    open: PositiveAmount
    high: PositiveAmount
    low: PositiveAmount
    close: PositiveAmount
    volume: NonnegativeAmount
    quote_volume: NonnegativeAmount | None = None
    is_closed: StrictBool = True

    @model_validator(mode="after")
    def consistent_window(self) -> Self:
        if self.closed_at <= self.opened_at:
            raise ValueError("candle requires a positive time window")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("candle high/low do not bound open/close")
        return self


class TradeTick(DomainModel):
    kind: Literal["trade"] = "trade"
    symbol: Identifier
    trade_id: Identifier
    price: PositiveAmount
    quantity: PositiveAmount


class BookTicker(DomainModel):
    kind: Literal["book"] = "book"
    symbol: Identifier
    bid: PositiveAmount
    ask: PositiveAmount
    bid_quantity: NonnegativeAmount
    ask_quantity: NonnegativeAmount

    @model_validator(mode="after")
    def ordered_quotes(self) -> Self:
        if self.bid > self.ask:
            raise ValueError("book quotes are crossed")
        return self


class MarketEvent(DomainModel):
    event_id: Identifier
    symbol: Identifier
    source: Literal["fake", "replay", "binance_direct", "binance_agent_os"]
    occurred_at: UtcDateTime
    received_at: UtcDateTime
    time_quality: Literal["exchange", "received"]
    stream_sequence: int | None = Field(default=None, strict=True, ge=0)
    payload: Annotated[TradeTick | BookTicker | Candle, Field(discriminator="kind")]

    @model_validator(mode="after")
    def consistent_source(self) -> Self:
        if self.payload.symbol != self.symbol:
            raise ValueError("event payload has a different symbol")
        if self.time_quality == "received" and self.occurred_at != self.received_at:
            raise ValueError("received-time event must preserve actual reception time")
        return self


class AggregateWindow(DomainModel):
    interval_seconds: Literal[1, 5, 60]
    candle: Candle
    trade_count: int = Field(strict=True, ge=1)
    complete: StrictBool

    @model_validator(mode="after")
    def consistent_interval(self) -> Self:
        if (
            not self.candle.is_closed
            or (self.candle.closed_at - self.candle.opened_at).total_seconds()
            != self.interval_seconds
        ):
            raise ValueError("aggregate must be a closed window of its declared duration")
        return self


class BufferUpdate(DomainModel):
    event_id: Identifier
    accepted: StrictBool = False
    duplicate: StrictBool = False
    late: StrictBool = False
    reason: Identifier | None = None

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        if sum((self.accepted, self.duplicate, self.late)) > 1:
            raise ValueError("buffer outcome cannot claim conflicting dispositions")
        if not (self.accepted or self.duplicate) and self.reason is None:
            raise ValueError("rejected buffer update requires a reason")
        return self


class MarketDataStatus(StrEnum):
    WARMING = "warming"
    READY = "ready"
    STALE = "stale"
    GAP = "gap"


class MarketSnapshot(DomainModel):
    symbol: Identifier
    as_of: UtcDateTime
    status: MarketDataStatus
    latest_received_at: UtcDateTime | None = None
    latest_quote_at: UtcDateTime | None = None
    book_as_of: UtcDateTime | None = None
    latest_trade: TradeTick | None = None
    book: BookTicker | None = None
    candles: tuple[Candle, ...] = ()

    @model_validator(mode="after")
    def no_lookahead(self) -> Self:
        if self.latest_received_at is not None and self.latest_received_at > self.as_of:
            raise ValueError("snapshot includes a future reception time")
        if self.latest_quote_at is not None and self.latest_quote_at > self.as_of:
            raise ValueError("snapshot includes a future quote time")
        if self.book_as_of is not None and self.book_as_of > self.as_of:
            raise ValueError("snapshot includes a future book time")
        if any(
            observation.symbol != self.symbol
            for observation in (self.latest_trade, self.book)
            if observation is not None
        ):
            raise ValueError("snapshot quote has a different symbol")
        previous_close = None
        for candle in self.candles:
            if (
                candle.symbol != self.symbol
                or not candle.is_closed
                or candle.closed_at > self.as_of
            ):
                raise ValueError("snapshot contains another symbol or unfinished/future candle")
            if previous_close is not None and candle.opened_at < previous_close:
                raise ValueError("snapshot candles are overlapping or unordered")
            previous_close = candle.closed_at
        if self.status == MarketDataStatus.READY:
            if self.latest_received_at is None or (self.latest_trade is None and self.book is None):
                raise ValueError("ready market context requires a current quote")
        return self


class FeatureSnapshot(DomainModel):
    symbol: Identifier
    as_of: UtcDateTime
    snapshot_id: Identifier
    algorithm_version: Identifier = "features-v1"
    warmup_ready: StrictBool = False
    interval_return: FiniteDecimal | None = None
    ema_fast: PositiveAmount | None = None
    ema_slow: PositiveAmount | None = None
    atr: NonnegativeAmount | None = None
    vwap: PositiveAmount | None = None
    volatility: NonnegativeAmount | None = None
    volume_change: FiniteDecimal | None = None
    spread: NonnegativeAmount | None = None

    @model_validator(mode="after")
    def honest_warmup(self) -> Self:
        if self.warmup_ready and any(
            value is None
            for value in (
                self.interval_return,
                self.ema_fast,
                self.ema_slow,
                self.atr,
                self.vwap,
                self.volatility,
                self.volume_change,
                self.spread,
            )
        ):
            raise ValueError("ready features require all defined values")
        return self
