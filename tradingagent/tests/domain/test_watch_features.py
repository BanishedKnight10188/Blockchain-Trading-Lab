"""Known answers, provenance and point-in-time feature boundaries."""

import importlib
from decimal import Decimal

import pytest

from tests.fixtures.watch_cases import candles, last_volume


@pytest.mark.parametrize("interval", ["1m", "5m"])
def test_volume_baseline_excludes_current(interval):
    compute = importlib.import_module(
        "agent_platform.domain.watch_features"
    ).calculate_watch_features
    bars = last_volume(candles(21, interval=interval), "150")
    result = compute(bars, interval)
    assert result.volume_ratio_20 == Decimal("1.5")
    assert result.ema_12 == Decimal("105")
    assert result.atr_14 == Decimal("20")
    assert result.ema_26 is None


def test_future_data_does_not_change_point_in_time_features():
    compute = importlib.import_module(
        "agent_platform.domain.watch_features"
    ).calculate_watch_features
    bars = last_volume(candles(21), "150")
    before = compute(bars, "1m")
    future = candles(1)[0].model_copy(update={"volume": Decimal("999999")})
    assert compute((*bars, future)[:21], "1m") == before


def test_zero_volume_and_warmup_are_explicit():
    compute = importlib.import_module(
        "agent_platform.domain.watch_features"
    ).calculate_watch_features
    result = compute(last_volume(candles(21, volume="0"), "150"), "1m")
    assert result.volume_ratio_20 is None
    assert "volume_ratio_20" in result.unavailable_metrics
    short = compute(candles(2), "1m")
    assert short.ema_12 is None and short.atr_14 is None


def test_gap_restarts_indicator_warmup():
    compute = importlib.import_module(
        "agent_platform.domain.watch_features"
    ).calculate_watch_features
    bars = candles(26)
    result = compute((*bars[:20], *bars[21:]), "1m")
    assert result.ema_12 is None and result.volume_ratio_20 is None


def test_wrong_interval_or_unfinished_bar_is_rejected():
    compute = importlib.import_module(
        "agent_platform.domain.watch_features"
    ).calculate_watch_features
    with pytest.raises(ValueError):
        compute(candles(interval="5m"), "1m")
    with pytest.raises(ValueError):
        compute((candles(1)[0].model_copy(update={"is_closed": False}),), "1m")
