"""Market context preserves gaps, event-time quality and the no-lookahead boundary."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
def market():
    return importlib.import_module("agent_platform.domain.market")


def candle(market, **changes):
    return market.Candle(
        **{
            "symbol": "BTCUSDT",
            "opened_at": NOW - timedelta(minutes=1),
            "closed_at": NOW,
            "open": "100",
            "high": "110",
            "low": "90",
            "close": "105",
            "volume": "1",
            **changes,
        }
    )


def test_snapshot_cannot_include_future_or_open_candles(market):
    for observation in (
        candle(market, closed_at=NOW + timedelta(seconds=1)),
        candle(market, is_closed=False),
    ):
        with pytest.raises(ValueError):
            market.MarketSnapshot(
                symbol="BTCUSDT", as_of=NOW, status="warming", candles=(observation,)
            )


def test_snapshot_rejects_other_symbols_and_duplicate_windows(market):
    for observations in ((candle(market, symbol="ETHUSDT"),), (candle(market),) * 2):
        with pytest.raises(ValueError):
            market.MarketSnapshot(
                symbol="BTCUSDT", as_of=NOW, status="warming", candles=observations
            )


def test_empty_context_is_warming_instead_of_fabricated_zero(market):
    snapshot = market.MarketSnapshot(symbol="BTCUSDT", as_of=NOW, status="warming")
    assert snapshot.latest_trade is None
    assert snapshot.book is None
    assert snapshot.candles == ()
    with pytest.raises(ValueError):
        market.MarketSnapshot(symbol="BTCUSDT", as_of=NOW, status="ready")


def test_received_time_cannot_be_mislabelled_as_exchange_time(market):
    values = {
        "event_id": "book-1",
        "symbol": "BTCUSDT",
        "source": "fake",
        "occurred_at": NOW,
        "received_at": NOW + timedelta(seconds=1),
        "time_quality": "received",
        "payload": {
            "kind": "book",
            "symbol": "BTCUSDT",
            "bid": "100",
            "ask": "101",
            "bid_quantity": "1",
            "ask_quantity": "1",
        },
    }
    with pytest.raises(ValueError):
        market.MarketEvent(**values)
    event = market.MarketEvent(**{**values, "occurred_at": values["received_at"]})
    restored = market.MarketEvent.model_validate_json(event.model_dump_json())
    assert restored.time_quality == "received"
    assert restored.payload.bid == 100


def test_crossed_book_is_not_a_valid_quote(market):
    with pytest.raises(ValueError):
        market.BookTicker(
            symbol="BTCUSDT", bid="101", ask="100", bid_quantity="1", ask_quantity="1"
        )


def test_event_payload_cannot_change_its_symbol(market):
    with pytest.raises(ValueError):
        market.MarketEvent(
            event_id="trade-1",
            source="fake",
            symbol="BTCUSDT",
            occurred_at=NOW,
            received_at=NOW,
            time_quality="exchange",
            payload={
                "kind": "trade",
                "symbol": "ETHUSDT",
                "trade_id": "1",
                "price": "100",
                "quantity": "1",
            },
        )


def test_feature_warmup_is_explicit_and_json_safe(market):
    features = market.FeatureSnapshot(symbol="BTCUSDT", as_of=NOW, snapshot_id="snapshot-1")
    assert features.warmup_ready is False
    assert features.ema_fast is None
    assert features.atr is None
    restored = market.FeatureSnapshot.model_validate_json(features.model_dump_json())
    assert restored == features
    with pytest.raises(ValueError):
        market.FeatureSnapshot(
            symbol="BTCUSDT", as_of=NOW, snapshot_id="snapshot-1", warmup_ready=True
        )
