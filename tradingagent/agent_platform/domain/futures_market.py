"""Owned public futures facts, distinct from wallet and model state."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_serializer, model_validator

from .futures_values import (
    Amount,
    FuturesFunding,
    FuturesQuote,
    Price,
    SignedAmount,
    Source,
)
from .models import DomainModel, UtcDateTime
from .session_market import FuturesSymbol, PerpetualContract


class _FuturesFact(DomainModel):
    model_config = ConfigDict(revalidate_instances="always")


class FuturesClockEvidence(_FuturesFact):
    source: Literal["ntp.aliyun.com", "ntp1.aliyun.com"]
    synchronized_at: UtcDateTime
    uncertainty_ms: float = Field(ge=0, le=50, allow_inf_nan=False)
    local_offset_ms: float = Field(allow_inf_nan=False)
    age_seconds: float = Field(ge=0, le=180, allow_inf_nan=False)


class FuturesMarketSnapshot(_FuturesFact):
    quote: FuturesQuote
    index_price: Price
    displayed_funding_rate: SignedAmount = Field(ge=Decimal("-0.1"), le=Decimal("0.1"))
    next_funding_at: UtcDateTime
    bid_quantity: Amount = Field(gt=0)
    ask_quantity: Amount = Field(gt=0)
    recent_quotes: tuple[FuturesQuote, ...] = Field(default=(), max_length=30)
    clock_evidence: FuturesClockEvidence | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_shape(self, handler):
        result = handler(self)
        if self.clock_evidence is None:
            result.pop("clock_evidence", None)
        return result

    @field_validator("quote", mode="before")
    @classmethod
    def owned_quote(cls, value):
        return value.model_dump(warnings=False) if isinstance(value, FuturesQuote) else value

    @model_validator(mode="after")
    def usable_exchange_evidence(self) -> Self:
        if (
            self.quote.received_at - min(self.quote.mark_at, self.quote.book_at)
            > timedelta(seconds=5)
            or self.next_funding_at <= self.quote.mark_at
        ):
            raise ValueError("futures quote is stale or its funding schedule is invalid")
        if self.clock_evidence and (
            self.quote.source != "binance_futures_public"
            or self.clock_evidence.synchronized_at
            > self.quote.received_at + timedelta(milliseconds=50)
        ):
            raise ValueError("clock proof does not belong to this public quote")
        previous = None
        for quote in self.recent_quotes:
            if (
                quote.symbol != self.quote.symbol
                or quote.source != self.quote.source
                or quote.received_at > self.quote.received_at
                or (previous is not None and quote.received_at <= previous)
            ):
                raise ValueError("recent quote history has invalid identity or chronology")
            previous = quote.received_at
        return self


class FuturesContractRules(_FuturesFact):
    contract: PerpetualContract
    source: Literal["binance_futures_public", "offline_replay"] = "binance_futures_public"
    captured_at: UtcDateTime
    price_tick: Price
    min_price: Amount
    max_price: Amount
    lot_step: Amount = Field(gt=0)
    lot_min_qty: Amount = Field(gt=0)
    lot_max_qty: Amount = Field(gt=0)
    market_step: Amount = Field(gt=0)
    market_min_qty: Amount = Field(gt=0)
    market_max_qty: Amount = Field(gt=0)
    min_notional: Amount = Field(gt=0)
    percent_down: Amount = Field(gt=0, le=1)
    percent_up: Amount = Field(ge=1)
    market_take_bound: Amount = Field(lt=1)

    @field_validator("contract", mode="before")
    @classmethod
    def owned_contract(cls, value):
        return value.model_dump(warnings=False) if isinstance(value, PerpetualContract) else value

    @model_validator(mode="after")
    def consistent_public_rules(self) -> Self:
        if (
            (self.max_price > 0 and max(self.min_price, self.price_tick) > self.max_price)
            or max(self.lot_min_qty, self.lot_step) > self.lot_max_qty
            or max(self.market_min_qty, self.market_step) > self.market_max_qty
        ):
            raise ValueError("public futures order bounds are inconsistent")
        return self


class FuturesFundingWindow(_FuturesFact):
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    symbol: FuturesSymbol
    source: Source = "binance_futures_public"
    requested_after: UtcDateTime
    requested_through: UtcDateTime
    captured_at: UtcDateTime
    events: tuple[FuturesFunding, ...] = Field(max_length=4000)

    @field_validator("events", mode="before")
    @classmethod
    def owned_events(cls, value):
        if type(value) not in (tuple, list) or len(value) > 4000:
            raise ValueError("funding events must be a bounded concrete batch")
        return tuple(
            event.model_dump(warnings=False) if isinstance(event, FuturesFunding) else event
            for event in value
        )

    @model_validator(mode="after")
    def bounded_complete_evidence(self) -> Self:
        if (
            not datetime(1970, 1, 1, tzinfo=UTC)
            <= self.requested_after
            <= self.requested_through
            <= self.captured_at
            or self.requested_through - self.requested_after > timedelta(days=31)
            or self.requested_after.microsecond % 1000
            or self.requested_through.microsecond % 1000
        ):
            raise ValueError("funding window is outside the supported replay bounds")
        previous = self.requested_after
        for event in self.events:
            if (
                event.symbol != self.symbol
                or event.source != self.source
                or event.settled_at.microsecond % 1000
                or not previous < event.settled_at <= self.requested_through
            ):
                raise ValueError("funding window contains wrong, duplicate or unordered evidence")
            previous = event.settled_at
        return self
