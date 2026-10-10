"""Auxiliary public observations; immutable core decisions keep their own evidence."""

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from .market import Candle, MarketDataStatus
from .models import DomainModel, PositiveAmount, UtcDateTime


class MarketRetentionPolicy(DomainModel):
    raw_days: int = Field(default=7, strict=True, ge=1, le=365)
    minute_days: int = Field(default=90, strict=True, ge=1, le=365)


class QuoteSample(DomainModel):
    kind: Literal["sample"] = "sample"
    price: PositiveAmount | None = None
    quote_at: UtcDateTime | None = None
    book_at: UtcDateTime | None = None
    bid: PositiveAmount | None = None
    ask: PositiveAmount | None = None
    status: MarketDataStatus

    @model_validator(mode="after")
    def coherent_quote(self):
        if (self.price is None) != (self.quote_at is None):
            raise ValueError("sample price requires its observation time")
        if (self.bid is None) != (self.ask is None) or (self.bid is None) != (self.book_at is None):
            raise ValueError("sample book requires complete quotes and time")
        if self.bid is not None and self.bid > self.ask:
            raise ValueError("sample quotes are crossed")
        return self


class MarketArchiveRecord(DomainModel):
    kind: Literal["raw", "minute"]
    mode: Literal["fake", "replay", "live_read_only"]
    source: Literal["fake", "replay", "binance_direct"]
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    event_at: UtcDateTime
    collected_at: UtcDateTime
    payload: Annotated[QuoteSample | Candle, Field(discriminator="kind")]

    @model_validator(mode="after")
    def valid_evidence(self) -> Self:
        if (
            self.source
            != {"fake": "fake", "replay": "replay", "live_read_only": "binance_direct"}[self.mode]
        ):
            raise ValueError("archive mode and provider must agree")
        if self.event_at > self.collected_at:
            raise ValueError("archive cannot contain future observations")
        if self.kind == "raw":
            if not isinstance(self.payload, QuoteSample):
                raise ValueError("raw archive requires a quote sample")
            if any(
                at is not None and at > self.collected_at
                for at in (self.payload.quote_at, self.payload.book_at)
            ):
                raise ValueError("sample includes future quote evidence")
        elif (
            not isinstance(self.payload, Candle)
            or not self.payload.is_closed
            or self.payload.symbol != "BTCUSDT"
            or self.payload.closed_at != self.event_at
            or (self.payload.closed_at - self.payload.opened_at).total_seconds() != 60
        ):
            raise ValueError("minute archive requires a completed BTCUSDT one-minute candle")
        return self

    @property
    def source_hash(self) -> str:
        body = self.model_dump(mode="json", exclude={"collected_at"})
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def record_id(self) -> str:
        scope = (self.kind, self.mode, self.source, self.symbol, self.event_at.isoformat())
        return (
            "market:"
            + hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        )


class RetentionReport(DomainModel):
    raw_removed: int = Field(strict=True, ge=0, le=1000)
    minute_removed: int = Field(strict=True, ge=0, le=1000)
    has_more: StrictBool
