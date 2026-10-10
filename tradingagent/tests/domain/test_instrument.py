"""Trading filters are versioned facts, not generic floating-point settings."""

import importlib

import pytest


def instrument_data():
    return {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "filter_version": "fixture-filters-v1",
        "price_tick": "0.01",
        "quantity_step": "0.00001",
        "min_quantity": "0.00001",
        "min_notional": "5",
    }


def test_instrument_filters_are_exact_and_owned():
    market = importlib.import_module("agent_platform.domain.market")
    instrument = market.Instrument(**instrument_data())
    assert market.Instrument.model_validate_json(instrument.model_dump_json()) == instrument


@pytest.mark.parametrize(
    "changes",
    [
        {"base_asset": "USDT"},
        {"quantity_step": "0"},
        {"price_tick": 0.01},
        {"max_quantity": "0.000001"},
        {"market_type": "margin"},
    ],
)
def test_instrument_rejects_inconsistent_filters(changes):
    market = importlib.import_module("agent_platform.domain.market")
    with pytest.raises(ValueError):
        market.Instrument(**{**instrument_data(), **changes})
