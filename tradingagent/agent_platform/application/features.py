"""Deterministic closed-minute indicators using a private Decimal34 context."""

from collections.abc import Sequence
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from hashlib import sha256

from agent_platform.domain.market import Candle, FeatureSnapshot, MarketSnapshot


def _period(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 120:
        raise ValueError("indicator period must be an integer between 1 and 120")
    return value


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    period = _period(period)
    if len(values) < period:
        return None
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        result = sum(values[:period], Decimal(0)) / period
        alpha = Decimal(2) / (period + 1)
        for value in values[period:]:
            result = alpha * value + (1 - alpha) * result
        return result


def atr(candles: Sequence[Candle], period: int) -> Decimal | None:
    period = _period(period)
    if len(candles) < period:
        return None
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        ranges = []
        previous_close = None
        for candle in candles:
            width = candle.high - candle.low
            if previous_close is not None:
                width = max(
                    width, abs(candle.high - previous_close), abs(candle.low - previous_close)
                )
            ranges.append(width)
            previous_close = candle.close
        result = sum(ranges[:period], Decimal(0)) / period
        for width in ranges[period:]:
            result = (result * (period - 1) + width) / period
        return result


class FeatureService:
    def __init__(
        self,
        *,
        ema_fast_period: int = 12,
        ema_slow_period: int = 26,
        atr_period: int = 14,
        volume_window: int = 5,
        lookback: int = 120,
    ):
        self.fast, self.slow, self.atr_period = map(
            _period, (ema_fast_period, ema_slow_period, atr_period)
        )
        self.volume_window, self.lookback = _period(volume_window), _period(lookback)
        if self.lookback < max(3, self.fast, self.slow, self.atr_period, 2 * self.volume_window):
            raise ValueError("feature lookback cannot satisfy the configured warmup")
        self.version = (
            f"features-v1:ema{self.fast}-{self.slow}:atr{self.atr_period}"
            f":volume{self.volume_window}:lookback{self.lookback}"
        )

    def compute(self, snapshot: MarketSnapshot) -> FeatureSnapshot:
        # A gap invalidates the earlier seed: use only the contiguous tail and
        # require warmup again, without filling missing candles.
        candles: list[Candle] = []
        for candle in snapshot.candles[-self.lookback :]:
            if candle.closed_at - candle.opened_at != timedelta(minutes=1):
                raise ValueError("minute features require complete one-minute candles")
            if candles and candles[-1].closed_at != candle.opened_at:
                candles = []
            candles.append(candle)
        prices = tuple(candle.close for candle in candles)
        with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
            returns = tuple(end / start - 1 for start, end in zip(prices, prices[1:], strict=False))
            interval_return = prices[-1] / prices[0] - 1 if len(prices) >= 2 else None
            volatility = None
            if len(returns) >= 2:
                average = sum(returns, Decimal(0)) / len(returns)
                variance = sum(((value - average) ** 2 for value in returns), Decimal(0)) / (
                    len(returns) - 1
                )
                volatility = variance.sqrt()
            volume = sum((candle.volume for candle in candles), Decimal(0))
            vwap = None
            if (
                candles
                and volume > 0
                and all(candle.quote_volume is not None for candle in candles)
            ):
                vwap = sum((candle.quote_volume for candle in candles), Decimal(0)) / volume
            volume_change = None
            window = self.volume_window
            if len(candles) >= 2 * window:
                old = sum((candle.volume for candle in candles[-2 * window : -window]), Decimal(0))
                new = sum((candle.volume for candle in candles[-window:]), Decimal(0))
                if old > 0:
                    volume_change = new / old - 1
            spread = None
            if (
                snapshot.book
                and snapshot.book_as_of is not None
                and (snapshot.as_of - snapshot.book_as_of).total_seconds() <= 5
            ):
                spread = snapshot.book.ask - snapshot.book.bid
        values = dict(
            interval_return=interval_return,
            ema_fast=ema(prices, self.fast),
            ema_slow=ema(prices, self.slow),
            atr=atr(candles, self.atr_period),
            vwap=vwap,
            volatility=volatility,
            volume_change=volume_change,
            spread=spread,
        )
        identity = sha256((self.version + snapshot.model_dump_json()).encode()).hexdigest()
        return FeatureSnapshot(
            symbol=snapshot.symbol,
            as_of=snapshot.as_of,
            snapshot_id="features:" + identity,
            algorithm_version=self.version,
            warmup_ready=snapshot.status == "ready"
            and bool(candles)
            and candles[-1].closed_at == snapshot.as_of.replace(second=0, microsecond=0)
            and snapshot.latest_quote_at is not None
            and (snapshot.as_of - snapshot.latest_quote_at).total_seconds() <= 5
            and all(value is not None for value in values.values()),
            **values,
        )
