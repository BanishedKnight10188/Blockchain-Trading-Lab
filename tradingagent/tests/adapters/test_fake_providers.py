"""Recorded providers expose owned contracts and cannot invent later observations."""

import importlib
from datetime import timedelta

import pytest

from agent_platform.domain.market import MarketEvent
from agent_platform.domain.model_calls import ModelRequest, ModelResponse
from agent_platform.ports.market import MarketDataPort
from agent_platform.ports.model import ModelPort
from tests.domain.test_decisions import NOW, snapshot_data
from tests.replay.test_replay import event


def request():
    return ModelRequest(
        request_id="recorded-1",
        snapshot=snapshot_data(),
        purpose="advisory",
        route={
            "route_id": "fake-route",
            "kind": "economy",
            "purpose": "advisory",
            "reason": "offline fixture",
            "model_version": "recorded-model-v1",
        },
        deadline=NOW + timedelta(seconds=15),
        max_output_tokens=200,
        prompt_version="test-v1",
    )


def response():
    return ModelResponse(
        request_id="recorded-1",
        assessment={"action": "hold", "explanation": "录制测试响应", "source": "fake"},
        usage={
            "request_id": "recorded-1",
            "route_id": "fake-route",
            "model_version": "recorded-model-v1",
            "input_tokens": 20,
            "output_tokens": 10,
            "estimated_cost_usd": "0",
            "actual_cost_usd": "0",
            "billing_status": "confirmed",
            "recorded_at": NOW,
        },
    )


@pytest.mark.asyncio
async def test_recorded_model_is_exact_and_unknown_or_changed_request_is_rejected():
    module = importlib.import_module("agent_platform.adapters.fake.model")
    model = module.FakeModel(((request(), response()),))
    assert isinstance(model, ModelPort)
    assert await model.generate(request()) == response()
    assert await model.generate(request()) == response()
    assert model.network_calls == 0
    for changed in ({"request_id": "unknown"}, {"max_output_tokens": 201}):
        with pytest.raises(ValueError):
            await model.generate(ModelRequest.model_validate({**request().model_dump(), **changed}))


@pytest.mark.asyncio
async def test_fake_market_latest_excludes_the_next_undelivered_event():
    module = importlib.import_module("agent_platform.adapters.fake.market")
    market = module.FakeMarket((event(), event("second", 2, "70000")))
    assert isinstance(market, MarketDataPort)
    with pytest.raises(ValueError):
        await market.latest("BTCUSDT")
    stream = market.stream(("BTCUSDT",))
    await anext(stream)
    assert (await market.latest("BTCUSDT")).latest_trade.price == 60000
    await anext(stream)
    assert (await market.latest("BTCUSDT")).latest_trade.price == 70000


@pytest.mark.asyncio
async def test_new_book_supersedes_old_trade_and_candle_does_not_refresh_quote_time():
    module = importlib.import_module("agent_platform.adapters.fake.market")
    market = module.FakeMarket()
    market.observe(event())
    book = MarketEvent(
        event_id="book-2",
        symbol="BTCUSDT",
        source="fake",
        occurred_at=NOW + timedelta(seconds=2),
        received_at=NOW + timedelta(seconds=2),
        time_quality="exchange",
        payload={
            "kind": "book",
            "symbol": "BTCUSDT",
            "bid": "70000",
            "ask": "70001",
            "bid_quantity": "1",
            "ask_quantity": "1",
        },
    )
    current = market.observe(book)
    assert current.latest_trade is None
    candle = MarketEvent(
        event_id="candle-10",
        symbol="BTCUSDT",
        source="fake",
        occurred_at=NOW + timedelta(seconds=10),
        received_at=NOW + timedelta(seconds=10),
        time_quality="exchange",
        payload={
            "kind": "candle",
            "symbol": "BTCUSDT",
            "opened_at": NOW,
            "closed_at": NOW + timedelta(seconds=10),
            "open": "70000",
            "high": "70000",
            "low": "70000",
            "close": "70000",
            "volume": "1",
        },
    )
    current = market.observe(candle)
    assert current.latest_received_at == book.received_at
    assert current.status == "stale"


@pytest.mark.asyncio
async def test_delayed_exchange_quote_is_stale_when_received_and_cannot_fill():
    from tests.adapters.test_paper import intent, simulator

    module = importlib.import_module("agent_platform.adapters.fake.market")
    delayed = MarketEvent.model_validate(
        {**event(offset=1).model_dump(), "received_at": NOW + timedelta(seconds=10)}
    )
    current = module.FakeMarket().observe(delayed)
    assert current.status == "stale"
    with pytest.raises(ValueError):
        await simulator().simulate(intent(), current)
