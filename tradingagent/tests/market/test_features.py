"""Hand-calculated indicators and honest warmup on closed minute candles."""

import importlib
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from agent_platform.domain.market import Candle, MarketSnapshot
from tests.domain.test_decisions import NOW


def candle(index, price="100", volume="1", *, quote_volume=None, **ohlc):
    data = dict(
        symbol="BTCUSDT",
        opened_at=NOW + timedelta(minutes=index),
        closed_at=NOW + timedelta(minutes=index + 1),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
    )
    data.update(ohlc)
    if quote_volume is not None:
        data["quote_volume"] = quote_volume
    return Candle(**data)


def market(candles):
    timestamp = candles[-1].closed_at if candles else NOW
    return MarketSnapshot(
        symbol="BTCUSDT",
        as_of=timestamp,
        latest_received_at=timestamp,
        latest_quote_at=timestamp,
        book_as_of=timestamp,
        status="ready",
        candles=tuple(candles),
        book={
            "symbol": "BTCUSDT",
            "bid": "129",
            "ask": "131",
            "bid_quantity": "1",
            "ask_quantity": "1",
        },
    )


def service(**extra):
    module = importlib.import_module("agent_platform.application.features")
    return module.FeatureService(
        ema_fast_period=3, ema_slow_period=3, atr_period=3, volume_window=1, **extra
    )


def test_ema_uses_sma_seed_then_exponential_smoothing():
    module = importlib.import_module("agent_platform.application.features")
    assert module.ema(tuple(map(Decimal, ("100", "110", "120", "130"))), 3) == 120
    assert module.ema((Decimal("100"), Decimal("110")), 3) is None


def test_atr_uses_true_range_and_wilder_seed():
    module = importlib.import_module("agent_platform.application.features")
    facts = (
        candle(0, "102", open="100", high="103", low="100"),
        candle(1, "105", open="102", high="108", low="102"),
        candle(2, "104", open="105", high="106", low="103"),
        candle(3, "110", open="104", high="114", low="104"),
    )
    assert module.atr(facts[:3], 3) == 4
    assert module.atr(facts, 3) == 6
    assert module.atr(facts[:2], 3) is None


def test_vwap_uses_actual_quote_volume_and_never_guesses_from_close_price():
    features = service().compute(
        market((candle(0, "100", quote_volume="100"), candle(1, "110", quote_volume="110")))
    )
    assert features.vwap == 105
    assert not features.warmup_ready
    missing = service().compute(market((candle(0), candle(1, "110"))))
    assert missing.vwap is None


def test_return_sample_volatility_volume_change_and_spread():
    facts = (
        candle(0, "100", quote_volume="100"),
        candle(1, "110", quote_volume="110"),
        candle(2, "99", volume="2", quote_volume="198"),
    )
    features = service().compute(market(facts))
    assert features.interval_return == Decimal("-0.01")
    assert abs(features.volatility - Decimal("0.1414213562373095048801688724209698")) < Decimal(
        "1e-33"
    )
    assert features.volume_change == 1
    assert features.spread == 2
    assert features.warmup_ready


def test_empty_and_single_candle_do_not_turn_unknown_metrics_into_zero():
    for facts in ((), (candle(0, quote_volume="100"),)):
        features = service().compute(market(facts))
        assert not features.warmup_ready
        assert features.ema_fast is None
        assert features.atr is None
        assert features.volatility is None
        assert features.interval_return is None
        assert features.volume_change is None


def test_gap_resets_warmup_instead_of_treating_missing_minutes_as_contiguous():
    facts = tuple(
        candle(index, str(100 + index), quote_volume=str(100 + index)) for index in (0, 1, 3)
    )
    features = service().compute(market(facts))
    assert features.ema_fast is None
    assert features.atr is None
    assert not features.warmup_ready


def test_zero_previous_volume_has_no_ratio_and_no_vwap():
    facts = tuple(candle(index, volume="0", quote_volume="0") for index in range(3))
    features = service().compute(market(facts))
    assert features.vwap is None
    assert features.volume_change is None
    assert not features.warmup_ready


def test_stale_market_preserves_metrics_but_cannot_claim_ready():
    facts = tuple(
        candle(index, str(100 + index), quote_volume=str(100 + index)) for index in range(3)
    )
    stale = MarketSnapshot.model_validate({**market(facts).model_dump(), "status": "stale"})
    features = service().compute(stale)
    assert features.ema_fast is not None
    assert not features.warmup_ready


def test_ready_label_cannot_make_an_expired_or_unknown_book_current():
    facts = tuple(
        candle(index, str(100 + index), quote_volume=str(100 + index)) for index in range(3)
    )
    current = market(facts)
    for quote_at in (None, current.as_of - timedelta(seconds=6)):
        data = {**current.model_dump(), "latest_quote_at": quote_at, "book_as_of": quote_at}
        features = service().compute(MarketSnapshot.model_validate(data))
        assert features.spread is None
        assert not features.warmup_ready


def test_recent_quote_cannot_mask_missing_recent_completed_minutes():
    facts = tuple(
        candle(index, str(100 + index), quote_volume=str(100 + index)) for index in range(30)
    )
    timestamp = NOW + timedelta(hours=2)
    current = MarketSnapshot.model_validate(
        {
            **market(facts).model_dump(),
            "as_of": timestamp,
            "latest_quote_at": timestamp,
            "book_as_of": timestamp,
            "latest_received_at": timestamp,
        }
    )
    features = service().compute(current)
    assert features.ema_fast is not None
    assert not features.warmup_ready


def test_lookback_cannot_make_sample_volatility_permanently_unavailable():
    module = importlib.import_module("agent_platform.application.features")
    with pytest.raises(ValueError):
        module.FeatureService(
            ema_fast_period=1, ema_slow_period=1, atr_period=1, volume_window=1, lookback=2
        )


def test_features_are_deterministic_and_ignore_ambient_decimal_context():
    facts = tuple(
        candle(index, str(100 + index), quote_volume=str(100 + index)) for index in range(4)
    )
    original = service().compute(market(facts))
    with localcontext() as arithmetic:
        arithmetic.prec = 3
        assert service().compute(market(facts)) == original
    assert service(lookback=3).compute(market(facts)).snapshot_id != original.snapshot_id


@pytest.mark.parametrize("period", [0, -1, True, 3.0])
def test_invalid_feature_periods_are_rejected(period):
    module = importlib.import_module("agent_platform.application.features")
    with pytest.raises(ValueError):
        module.FeatureService(ema_fast_period=period)
