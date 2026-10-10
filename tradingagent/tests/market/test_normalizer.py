"""Official Spot wire fields map to exact owned types with real time quality."""

import importlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.domain.test_decisions import NOW

MS = 1791158400000


@pytest.mark.parametrize("amount", ["1e999999999", "1" * 129, "1e-8", "+1", " 1 "])
def test_wire_amounts_require_bounded_fixed_point_decimal_strings(amount):
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    with pytest.raises(module.NormalizationError):
        module.normalize_stream(trade(p=amount), NOW + timedelta(seconds=2))


def test_synthetic_official_format_fixture_maps_without_a_transport():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    source = Path(__file__).parents[1] / "fixtures" / "binance_market.jsonl"
    with source.open(encoding="utf-8") as stream:
        inputs = (json.loads(line) for line in stream)
        events = tuple(
            module.normalize_stream(item["payload"], datetime.fromisoformat(item["received_at"]))
            for item in inputs
        )
    assert tuple(event.payload.kind for event in events) == ("trade", "book", "candle")
    assert events[0].occurred_at == NOW
    assert events[1].time_quality == "received"
    assert events[2].payload.is_closed


def trade(**extra):
    return {
        "e": "trade",
        "E": MS + 1000,
        "s": "BTCUSDT",
        "t": 7,
        "T": MS,
        "p": "60000.01",
        "q": "0.001",
        "m": False,
        **extra,
    }


def kline(closed=False, **extra):
    return {
        "e": "kline",
        "E": MS + 60001,
        "s": "BTCUSDT",
        "k": {
            "s": "BTCUSDT",
            "i": "1m",
            "t": MS,
            "T": MS + 59999,
            "o": "60000",
            "h": "61000",
            "l": "59000",
            "c": "60500",
            "v": "1",
            "q": "60200",
            "x": closed,
            **extra,
        },
    }


def test_trade_uses_trade_time_instead_of_later_publication_time():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    result = module.normalize_stream(trade(), NOW + timedelta(seconds=2))
    assert result.occurred_at == NOW
    assert result.time_quality == "exchange"
    assert result.payload.trade_id == "7"
    assert str(result.payload.price) == "60000.01"
    assert result.source == "binance_direct"


def test_book_without_exchange_timestamp_keeps_actual_received_time():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    data = {"u": 8, "s": "BTCUSDT", "b": "60000", "B": "1", "a": "60001", "A": "2"}
    result = module.normalize_stream({"stream": "btcusdt@bookTicker", "data": data}, NOW)
    assert result.occurred_at == result.received_at == NOW
    assert result.time_quality == "received"
    assert result.payload.ask == 60001


def test_kline_half_open_window_quote_volume_and_final_event_have_distinct_identity():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    partial = module.normalize_stream(kline(), NOW + timedelta(minutes=1, milliseconds=2))
    final = module.normalize_stream(kline(True), NOW + timedelta(minutes=1, milliseconds=2))
    assert not partial.payload.is_closed
    assert final.payload.is_closed
    assert final.payload.opened_at == NOW
    assert final.payload.closed_at == NOW + timedelta(minutes=1)
    assert final.payload.quote_volume == 60200
    assert partial.event_id != final.event_id


def test_combined_stream_type_and_minute_interval_must_match_the_payload():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    for raw in (
        {"stream": "btcusdt@bookTicker", "data": trade()},
        kline(True, T=MS + 999),
    ):
        with pytest.raises(module.NormalizationError):
            module.normalize_stream(raw, NOW + timedelta(minutes=1))


def test_closed_flag_cannot_claim_a_future_completed_window():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    raw = kline(True)
    raw["E"] = MS + 30000
    with pytest.raises(module.NormalizationError):
        module.normalize_stream(raw, NOW + timedelta(seconds=30))


@pytest.mark.parametrize(
    "defect", ["float", "bool_id", "missing", "symbol", "combined_symbol", "interval"]
)
def test_bad_wire_values_are_rejected_without_echoing_raw_payload(defect):
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    data = trade()
    if defect == "float":
        data["p"] = 60000.01
    if defect == "bool_id":
        data["t"] = True
    if defect == "missing":
        data.pop("T")
    if defect == "symbol":
        data["s"] = "ETHUSDT"
    if defect == "combined_symbol":
        data = {"stream": "ethusdt@trade", "data": data}
    if defect == "interval":
        data = kline(i="5m")
    data["api_secret"] = "private-fixture-value"
    with pytest.raises(module.NormalizationError) as failure:
        module.normalize_stream(data, NOW)
    assert "private-fixture-value" not in str(failure.value)


def test_rest_kline_preserves_history_but_marks_the_current_window_unfinished():
    module = importlib.import_module("agent_platform.adapters.binance_direct.normalizer")
    row = [MS, "60000", "61000", "59000", "60500", "1", MS + 59999, "60200", 1, "0", "0", "0"]
    partial = module.normalize_kline_row(row, "BTCUSDT", NOW + timedelta(seconds=30))
    final = module.normalize_kline_row(row, "BTCUSDT", NOW + timedelta(minutes=1))
    assert not partial.payload.is_closed
    assert final.payload.is_closed
    assert final.payload.quote_volume == 60200
    assert final.time_quality == "received"
