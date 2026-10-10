"""Decimal34 EMA and Wilder ATR; gaps discard the earlier warmup seed."""

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from .market import Candle
from .watches import WATCH_LOOKBACK, WatchFeatures, WatchInterval, interval_delta


def validate_candles(candles: tuple[Candle, ...], interval: WatchInterval) -> None:
    step = interval_delta(interval)
    from datetime import timedelta

    for candle in candles:
        if not candle.is_closed or candle.closed_at != candle.opened_at + step - timedelta(
            milliseconds=1
        ):
            raise ValueError("watch requires closed native candles of the configured interval")
        if candle.opened_at.timestamp() % step.total_seconds() != 0:
            raise ValueError("watch candle is not aligned to a UTC boundary")
    if any(b.opened_at <= a.opened_at for a, b in zip(candles, candles[1:], strict=False)):
        raise ValueError("watch candles must be strictly chronological")


def calculate_watch_features(candles: tuple[Candle, ...], interval: WatchInterval) -> WatchFeatures:
    validate_candles(candles, interval)
    candles = candles[-WATCH_LOOKBACK:]
    tail: list[Candle] = []
    step = interval_delta(interval)
    for candle in candles:
        if tail and candle.opened_at != tail[-1].opened_at + step:
            tail = []
        tail.append(candle)
    # Same seeds, precision and smoothing as the existing FeatureService.
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        prices = [c.close for c in tail]

        def ema(period):
            if len(prices) < period:
                return None
            result = sum(prices[:period], Decimal(0)) / period
            alpha = Decimal(2) / (period + 1)
            for price in prices[period:]:
                result = alpha * price + (1 - alpha) * result
            return result

        atr = None
        if len(tail) >= 14:
            ranges = []
            previous = None
            for candle in tail:
                width = candle.high - candle.low
                if previous is not None:
                    width = max(width, abs(candle.high - previous), abs(candle.low - previous))
                ranges.append(width)
                previous = candle.close
            atr = sum(ranges[:14], Decimal(0)) / 14
            for width in ranges[14:]:
                atr = (atr * 13 + width) / 14
        ratio = None
        if len(tail) >= 21:
            baseline = sum((c.volume for c in tail[-21:-1]), Decimal(0)) / 20
            if baseline > 0:
                ratio = tail[-1].volume / baseline
        return WatchFeatures(volume_ratio_20=ratio, ema_12=ema(12), ema_26=ema(26), atr_14=atr)
