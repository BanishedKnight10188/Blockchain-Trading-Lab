"""Bounded native futures chart bars, independent from model input windows."""

from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import Field, model_validator

from .market import Candle
from .models import DomainModel, UtcDateTime
from .session_market import FuturesSymbol

KlineInterval = Literal[
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"
]
INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}
NATIVE_INTERVALS = (*INTERVAL_SECONDS, "1M")


def shift_open(opened: datetime, interval: KlineInterval, count: int = 1) -> datetime:
    if interval == "1M":
        month = opened.year * 12 + opened.month - 1 + count
        year, index = divmod(month, 12)
        return opened.replace(year=year, month=index + 1, day=1)
    return opened + timedelta(seconds=INTERVAL_SECONDS[interval] * count)


def candle_boundary(at: datetime, interval: KlineInterval) -> datetime:
    if interval == "1M":
        return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if interval == "1w":
        return at.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=at.weekday())
    seconds = INTERVAL_SECONDS[interval]
    return datetime.fromtimestamp(int(at.timestamp()) // seconds * seconds, UTC)


class ChartRequest(DomainModel):
    symbol: FuturesSymbol
    interval: KlineInterval = "1h"
    limit: int = Field(default=300, strict=True, ge=1, le=499)


class ChartHistory(DomainModel):
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    symbol: FuturesSymbol
    interval: KlineInterval
    source: Literal["binance_futures_public", "fake"]
    captured_at: UtcDateTime
    candles: tuple[Candle, ...] = Field(min_length=1, max_length=499)

    @model_validator(mode="after")
    def closed_consecutive_bars(self) -> Self:
        previous = None
        for candle in self.candles:
            opened = candle.opened_at
            if (
                candle.symbol != self.symbol
                or not candle.is_closed
                or candle.closed_at >= self.captured_at
                or candle.closed_at != shift_open(opened, self.interval) - timedelta(milliseconds=1)
                or (previous is not None and opened != shift_open(previous, self.interval))
                or opened.second
                or opened.microsecond
                or (self.interval == "3d" and (opened.hour or opened.minute))
                or (self.interval != "3d" and candle_boundary(opened, self.interval) != opened)
            ):
                raise ValueError("chart bars must match the selected closed interval without gaps")
            previous = opened
        return self
