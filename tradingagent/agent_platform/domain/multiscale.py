"""Native closed bars with exact provenance, bounded windows and no synthetic gaps."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from .market import Candle
from .models import DomainModel, UtcDateTime
from .session_market import FuturesSymbol

Interval = Literal["1s", "3m", "5m", "1h", "4h", "1d"]
Source = Literal["binance_futures_public", "fake"]
SECONDS = {"1s": 1, "3m": 180, "5m": 300, "1h": 3600, "4h": 14400, "1d": 86400}
BACKGROUND_WINDOWS = (("1d", 90), ("4h", 180), ("1h", 168), ("5m", 288))


def boundary(at, interval):
    seconds = SECONDS[interval]
    return datetime.fromtimestamp(int(at.timestamp()) // seconds * seconds, UTC)


class CandleWindow(DomainModel):
    symbol: FuturesSymbol
    interval: Interval
    source: Source
    captured_at: UtcDateTime
    requested_count: int = Field(strict=True, ge=1, le=288)
    candles: tuple[Candle, ...] = Field(default=(), max_length=288)

    @model_validator(mode="after")
    def consistent(self):
        if len(self.candles) > self.requested_count:
            raise ValueError("too many bars")
        step = timedelta(seconds=SECONDS[self.interval])
        previous = None
        for bar in self.candles:
            if (
                bar.symbol != self.symbol
                or not bar.is_closed
                or bar.opened_at != boundary(bar.opened_at, self.interval)
                or bar.closed_at != bar.opened_at + step - timedelta(milliseconds=1)
                or bar.closed_at >= self.captured_at
                or (previous is not None and bar.opened_at != previous + step)
            ):
                raise ValueError("bars require matching identity and consecutive closed intervals")
            previous = bar.opened_at
        return self

    @property
    def complete(self):
        return len(self.candles) == self.requested_count

    @property
    def fresh(self):
        return bool(self.candles) and (
            self.captured_at - (self.candles[-1].closed_at + timedelta(milliseconds=1))
            < timedelta(seconds=SECONDS[self.interval] + 2)
        )

    def compact(self):
        return [
            [
                b.opened_at.isoformat(),
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                str(b.volume),
            ]
            for b in self.candles
        ]

    @property
    def content_hash(self):
        data = {
            "symbol": self.symbol,
            "interval": self.interval,
            "source": self.source,
            "requested_count": self.requested_count,
            "candles": self.compact(),
        }
        return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def evidence(self):
        return {
            "interval": self.interval,
            "source": self.source,
            "requested_count": self.requested_count,
            "count": len(self.candles),
            "complete": self.complete,
            "fresh": self.fresh,
            "hash": self.content_hash,
            "start": self.candles[0].opened_at.isoformat() if self.candles else None,
            "end": (self.candles[-1].closed_at + timedelta(milliseconds=1)).isoformat()
            if self.candles
            else None,
        }

    def features(self):
        """Descriptive exact-decimal facts, with no forecast or invented volume."""
        if not self.candles:
            return {}
        with localcontext(Context(prec=80)):
            bars = self.candles
            high, low = max(b.high for b in bars), min(b.low for b in bars)
            returns = tuple(
                abs((b.close / a.close - 1) * 100) for a, b in zip(bars, bars[1:], strict=False)
            )
            volume = sum((b.volume for b in bars), Decimal(0))
            return {
                "change_from_first_open_percent": str((bars[-1].close / bars[0].open - 1) * 100),
                "high": str(high),
                "low": str(low),
                "last_close": str(bars[-1].close),
                "last_position_in_range": str((bars[-1].close - low) / (high - low))
                if high > low
                else None,
                "mean_absolute_close_change_percent": str(sum(returns, Decimal(0)) / len(returns))
                if returns
                else "0",
                "base_volume_sum": str(volume),
                "base_volume_mean": str(volume / len(bars)),
            }


class KlineBuffer:
    def __init__(self, symbol, source):
        # Validate the binding once using the same domain contract as every read.
        checked = CandleWindow(
            symbol=symbol,
            source=source,
            interval="1s",
            captured_at=datetime.now(UTC),
            requested_count=60,
        )
        self.symbol, self.source = checked.symbol, checked.source
        self._bars = {interval: () for interval in SECONDS}

    def accept(self, window):
        window = CandleWindow.model_validate_json(window.model_dump_json())
        if (window.symbol, window.source) != (self.symbol, self.source):
            raise ValueError("kline buffer identity changed")
        current = list(self._bars[window.interval])
        known = {bar.opened_at: bar for bar in current}
        step = timedelta(seconds=SECONDS[window.interval])
        for bar in window.candles:
            old = known.get(bar.opened_at)
            if old is not None:
                if old != bar:
                    raise ValueError("conflicting closed bar")
                continue
            if current and bar.opened_at <= current[-1].opened_at:
                continue
            if current and bar.opened_at != current[-1].opened_at + step:
                current = []  # Missing time is a gap, never an invented zero-volume bar.
            current.append(bar)
        self._bars[window.interval] = tuple(current[-288:])

    def window(self, interval, count, at):
        if interval not in SECONDS or type(count) is not int or not 1 <= count <= 288:
            raise ValueError("unsupported kline window")
        return CandleWindow(
            symbol=self.symbol,
            source=self.source,
            interval=interval,
            requested_count=count,
            captured_at=at,
            candles=self._bars[interval][-count:],
        )

    def seed(self, window):
        """REST can prepend missing history while a live closed bar is already present."""
        window = CandleWindow.model_validate_json(window.model_dump_json())
        if (window.symbol, window.source) != (self.symbol, self.source):
            raise ValueError("kline buffer identity changed")
        known = {b.opened_at: b for b in self._bars[window.interval]}
        for bar in window.candles:
            if bar.opened_at in known and known[bar.opened_at] != bar:
                raise ValueError("conflicting closed bar")
            known[bar.opened_at] = bar
        ordered = sorted(known.values(), key=lambda b: b.opened_at)
        suffix = []
        step = timedelta(seconds=SECONDS[window.interval])
        for bar in ordered:
            if suffix and bar.opened_at != suffix[-1].opened_at + step:
                suffix = []
            suffix.append(bar)
        self._bars[window.interval] = tuple(suffix[-288:])

    def disconnect(self):
        self._bars["1s"] = ()  # REST cannot repair seconds; reconnect must accumulate coverage.
