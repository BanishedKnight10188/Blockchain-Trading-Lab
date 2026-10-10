"""Explicit, deterministic local simulation without exchange execution."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.market import MarketSnapshot
from agent_platform.domain.paper import PaperIntent
from tests.domain.test_decisions import NOW


def quote(price="60000", *, offset=1):
    return MarketSnapshot(
        symbol="BTCUSDT",
        as_of=NOW + timedelta(seconds=offset),
        latest_received_at=NOW + timedelta(seconds=offset),
        latest_quote_at=NOW + timedelta(seconds=offset),
        book_as_of=NOW + timedelta(seconds=offset),
        status="ready",
        book={
            "symbol": "BTCUSDT",
            "bid": price,
            "ask": price,
            "bid_quantity": "1",
            "ask_quantity": "1",
        },
    )


def intent(identifier="intent-1", side="buy", quantity="0.001", **extra):
    return PaperIntent(
        intent_id=identifier,
        mode="paper",
        account_ref="paper:offline",
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        created_at=NOW,
        **extra,
    )


def simulator(**extra):
    module = importlib.import_module("agent_platform.adapters.paper.execution")
    return module.PaperSimulator(
        "paper:offline",
        balances={"USDT": "100", "BTC": "0"},
        fee_bps="10",
        slippage_bps="5",
        **extra,
    )


@pytest.mark.asyncio
async def test_fee_slippage_and_idempotent_fill_change_only_paper_balance():
    paper = simulator()
    first = await paper.simulate(intent(), quote())
    assert first.price == Decimal("60030")
    assert first.fee == Decimal("0.06003")
    assert first.fee_asset == "USDT"
    assert first.model_version == "paper-top-of-book-v1"
    assert paper.balance("USDT") == Decimal("39.90997")
    assert paper.balance("BTC") == Decimal("0.001")
    assert await paper.simulate(intent(), quote("70000", offset=2)) == first
    assert paper.balance("BTC") == Decimal("0.001")
    with pytest.raises(ValueError):
        await paper.simulate(intent(quantity="0.002"), quote())


@pytest.mark.asyncio
async def test_insufficient_quote_or_base_funds_do_not_create_a_fill():
    paper = simulator()
    before = paper.balances
    for request in (intent(quantity="0.01"), intent("sell-1", "sell")):
        with pytest.raises(ValueError, match="balance"):
            await paper.simulate(request, quote())
        assert paper.balances == before
    assert not paper.fills


@pytest.mark.asyncio
async def test_sell_uses_bid_and_deducts_fee_after_slippage():
    paper = simulator()
    await paper.simulate(intent(), quote())
    sold = await paper.simulate(intent("sell-1", "sell"), quote("61000"))
    assert sold.price == Decimal("60969.5")
    assert sold.fee == Decimal("0.0609695")
    assert paper.balance("BTC") == 0
    assert paper.balance("USDT") == Decimal("100.8185005")


@pytest.mark.asyncio
async def test_limit_price_and_chronology_are_respected():
    paper = simulator()
    for request, market in (
        (intent(limit_price="60000"), quote()),
        (intent(), quote(offset=-1)),
        (intent(), MarketSnapshot(symbol="BTCUSDT", as_of=NOW, status="warming")),
    ):
        with pytest.raises(ValueError):
            await paper.simulate(request, market)
    assert not paper.fills


@pytest.mark.asyncio
async def test_other_account_symbol_and_stale_quotes_cannot_fill():
    paper = simulator()
    other_account = PaperIntent.model_validate(
        {**intent().model_dump(), "account_ref": "paper:other"}
    )
    other_symbol = PaperIntent.model_validate({**intent().model_dump(), "symbol": "ETHUSDT"})
    stale = MarketSnapshot.model_validate(
        {**quote().model_dump(), "as_of": NOW + timedelta(seconds=10)}
    )
    for request, market in ((other_account, quote()), (other_symbol, quote()), (intent(), stale)):
        with pytest.raises(ValueError):
            await paper.simulate(request, market)
    assert not paper.fills


@pytest.mark.asyncio
async def test_current_trade_cannot_refresh_an_old_or_unknown_book_for_a_fill():
    for book_time in (None, NOW):
        paper = simulator()
        current = MarketSnapshot.model_validate(
            {
                **quote(offset=10).model_dump(),
                "book_as_of": book_time,
                "latest_trade": {
                    "kind": "trade",
                    "symbol": "BTCUSDT",
                    "trade_id": "new",
                    "price": "70000",
                    "quantity": "0.001",
                },
            }
        )
        with pytest.raises(ValueError):
            await paper.simulate(intent(), current)
        assert not paper.fills


def test_live_account_and_float_configuration_are_rejected():
    module = importlib.import_module("agent_platform.adapters.paper.execution")
    with pytest.raises(ValueError):
        module.PaperSimulator("live-account", balances={"USDT": "100"})
    with pytest.raises(ValueError):
        module.PaperSimulator("paper:offline", balances={"USDT": 100.0})


@pytest.mark.asyncio
async def test_simulation_is_independent_of_the_callers_decimal_context():
    from decimal import localcontext

    with localcontext() as arithmetic:
        arithmetic.prec = 3
        paper = simulator()
        fill = await paper.simulate(intent(quantity="0.00123456789"), quote())
    assert fill.price == Decimal("60030")
    assert fill.fee == Decimal("0.0741111104367")
    assert paper.balance("USDT") == Decimal("25.8147784528633")


@pytest.mark.asyncio
async def test_new_fill_cannot_use_funds_from_a_future_fill_but_old_retry_is_idempotent():
    paper = simulator()
    original = intent()
    bought = await paper.simulate(original, quote(offset=10))
    after = paper.balances
    with pytest.raises(ValueError):
        await paper.simulate(intent("earlier-sell", "sell"), quote("61000", offset=5))
    assert paper.balances == after
    assert await paper.simulate(original, quote(offset=1)) == bought
