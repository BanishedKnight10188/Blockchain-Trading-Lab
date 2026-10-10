"""Immutable analysis identity and bounded evidence, separate from Spot execution."""

from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from .market import Candle
from .models import DomainModel, Identifier, UtcDateTime


def futures_symbol(value: str) -> str:
    prefix = value.removesuffix("USDT")
    if (
        not value.endswith("USDT")
        or not prefix
        or len(value.encode()) > 128
        or any(not (c == "_" or c.isalnum()) for c in prefix)
        or any(c.isascii() and c.isalpha() and not c.isupper() for c in prefix)
    ):
        raise ValueError("invalid USDT contract symbol")
    return value


FuturesSymbol = Annotated[str, Field(strict=True, max_length=32), AfterValidator(futures_symbol)]


class SessionAnalysisTarget(DomainModel):
    market: Literal["spot", "usdt_perpetual"] = "spot"
    symbol: FuturesSymbol = "BTCUSDT"
    interval: Literal["1h"] = "1h"
    history_days: int = Field(default=7, strict=True)

    @model_validator(mode="after")
    def supported_identity(self) -> Self:
        if self.history_days not in (1, 7, 30):
            raise ValueError("history must be 1, 7 or 30 days")
        if self.market == "spot" and self.symbol != "BTCUSDT":
            raise ValueError("legacy Spot execution is BTCUSDT only")
        return self

    @property
    def legacy_spot(self) -> bool:
        return self.market == "spot" and self.symbol == "BTCUSDT"


class PerpetualContract(DomainModel):
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    symbol: FuturesSymbol
    base_asset: Identifier
    quote_asset: Literal["USDT"] = "USDT"
    margin_asset: Literal["USDT"] = "USDT"


class HistoricalMarketData(DomainModel):
    target: SessionAnalysisTarget
    contract: PerpetualContract
    source: Literal["binance_futures_public", "fake"]
    captured_at: UtcDateTime
    requested_start: UtcDateTime
    requested_end: UtcDateTime  # Exclusive UTC hour boundary.
    candles: tuple[Candle, ...] = Field(min_length=1, max_length=720)

    @model_validator(mode="after")
    def complete_closed_history(self) -> Self:
        from datetime import timedelta

        if (
            self.target.market != self.contract.market
            or self.target.symbol != self.contract.symbol
            or self.requested_end > self.captured_at
            or self.requested_end - self.requested_start != timedelta(days=self.target.history_days)
            or self.requested_start.minute
            or self.requested_start.second
            or self.requested_start.microsecond
            or len(self.candles) != self.target.history_days * 24
        ):
            raise ValueError("history identity or coverage is inconsistent")
        for i, candle in enumerate(self.candles):
            opened = self.requested_start + timedelta(hours=i)
            if (
                candle.symbol != self.target.symbol
                or not candle.is_closed
                or candle.opened_at != opened
                or candle.closed_at != opened + timedelta(hours=1, milliseconds=-1)
                or candle.closed_at >= self.requested_end
            ):
                raise ValueError("history must contain consecutive completed hourly bars")
        return self

    @property
    def content_hash(self) -> str:
        # Reception time is not part of the market evidence identity.
        return sha256(self.model_dump_json(exclude={"captured_at"}).encode()).hexdigest()
