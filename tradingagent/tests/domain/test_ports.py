"""Consumers use owned read-only facts and deterministic offline providers."""

import importlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agent_platform.domain.account import AccountSnapshot, ObservedTrade, TradeCursor
from agent_platform.ports.account import AccountPort


@pytest.mark.asyncio
async def test_fake_account_obeys_read_only_contract_and_cursor():
    fake = importlib.import_module("agent_platform.adapters.fake.account")
    now = datetime(2026, 10, 5, tzinfo=UTC)
    snapshot = AccountSnapshot(
        account_ref="offline-spot",
        as_of=now,
        balances=({"asset": "BTC", "free": "0.1", "locked": "0"},),
    )
    trades = tuple(
        ObservedTrade(
            account_ref="offline-spot",
            symbol="BTCUSDT",
            trade_id=str(trade_id),
            order_id=str(trade_id),
            side="buy",
            price="60000",
            quantity="0.001",
            fee="0",
            fee_asset="USDT",
            executed_at=now,
        )
        for trade_id in (10, 11, 12)
    )
    provider = fake.FakeAccount(snapshot, trades=trades)
    assert isinstance(provider, AccountPort)
    owned = await provider.snapshot("offline-spot")
    assert owned.balances[0].free == Decimal("0.1")
    batch = await provider.trades("offline-spot", "BTCUSDT", TradeCursor(last_trade_id="11"))
    assert [trade.trade_id for trade in batch.trades] == ["12"]
    assert batch.next_cursor.last_trade_id == "12"
    assert not hasattr(provider, "place_order")
    assert not hasattr(provider, "withdraw")
    assert not hasattr(provider, "transfer")
    with pytest.raises(ValueError):
        await provider.snapshot("different-account")


@pytest.mark.asyncio
async def test_unknown_cursor_is_rejected_instead_of_restarting_history():
    fake = importlib.import_module("agent_platform.adapters.fake.account")
    snapshot = AccountSnapshot(account_ref="offline-spot", as_of=datetime(2026, 10, 5, tzinfo=UTC))
    provider = fake.FakeAccount(snapshot)
    with pytest.raises(ValueError):
        await provider.trades("offline-spot", "BTCUSDT", TradeCursor(last_trade_id="missing"))
