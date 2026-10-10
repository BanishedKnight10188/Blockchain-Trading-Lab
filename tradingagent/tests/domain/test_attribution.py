"""Trade facts do not gain an Agent author from timing proximity."""

import importlib
from datetime import UTC, datetime

import pytest


def test_imported_trade_is_human_executed_without_decision_inference():
    account = importlib.import_module("agent_platform.domain.account")
    trade = account.ObservedTrade(
        account_ref="personal-spot",
        symbol="BTCUSDT",
        trade_id="789",
        order_id="123",
        side="buy",
        price="60000",
        quantity="0.001",
        fee="0.000001",
        fee_asset="BTC",
        executed_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    assert trade.executor == "human"
    reviews = importlib.import_module("agent_platform.domain.reviews")
    attribution = reviews.TradeAttribution(
        account_ref=trade.account_ref,
        symbol=trade.symbol,
        trade_id=trade.trade_id,
    )
    assert attribution.original_author == "unclassified"
    assert attribution.final_decision_maker == "unclassified"
    assert attribution.recommendation_id is None
    assert attribution.executor == "human"


def test_real_trade_cannot_claim_agent_execution():
    account = importlib.import_module("agent_platform.domain.account")
    with pytest.raises(ValueError):
        account.ObservedTrade(
            account_ref="personal-spot",
            symbol="BTCUSDT",
            trade_id="789",
            order_id="123",
            side="buy",
            price="60000",
            quantity="0.001",
            fee="0",
            fee_asset="USDT",
            executed_at=datetime(2026, 10, 5, tzinfo=UTC),
            executor="agent",
        )


def test_linking_a_recommendation_requires_human_confirmation():
    reviews = importlib.import_module("agent_platform.domain.reviews")
    values = {"account_ref": "personal-spot", "symbol": "BTCUSDT", "trade_id": "789"}
    with pytest.raises(ValueError):
        reviews.TradeAttribution(**values, recommendation_id="advice-1")
    confirmed = reviews.TradeAttribution(
        **values,
        recommendation_id="advice-1",
        user_confirmed=True,
        original_author="agent",
        final_decision_maker="human",
    )
    assert confirmed.original_author == "agent"
    assert confirmed.final_decision_maker == "human"
    assert confirmed.executor == "human"


def test_batch_cannot_mix_accounts_or_silently_reuse_trade_ids():
    account = importlib.import_module("agent_platform.domain.account")
    values = {
        "account_ref": "personal-spot",
        "symbol": "BTCUSDT",
        "trade_id": "789",
        "order_id": "123",
        "side": "buy",
        "price": "60000",
        "quantity": "0.001",
        "fee": "0",
        "fee_asset": "USDT",
        "executed_at": datetime(2026, 10, 5, tzinfo=UTC),
    }
    trade = account.ObservedTrade(**values)
    with pytest.raises(ValueError):
        account.TradeBatch(account_ref="other", symbol="BTCUSDT", trades=(trade,))
    with pytest.raises(ValueError):
        account.TradeBatch(account_ref="personal-spot", symbol="BTCUSDT", trades=(trade, trade))
