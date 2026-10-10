"""Sync uses real observation transactions with Fake AccountPort, no credentials."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.adapters.binance_direct.account_mapping import map_account, map_order, map_trade
from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.observations import SqliteObservationStore
from agent_platform.domain.account import TradeBatch, TradeCursor
from agent_platform.domain.reviews import TradeAttribution
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.adapters.test_account_mapping import account_wire, order_wire, trade_wire
from tests.domain.test_decisions import NOW


def module():
    return importlib.import_module("agent_platform.application.account_sync")


class FakeAccount:
    def __init__(self, clock):
        self.clock, self.calls, self.fail = clock, [], None
        self.free = "0.01"
        self.ids = [1]

    async def snapshot(self, ref):
        self.calls.append("snapshot")
        if self.fail == "snapshot":
            raise module().AccountReadUnavailable("credentials")
        data = account_wire()
        data["balances"][0]["free"] = self.free
        return map_account(data, ref, self.clock.utcnow())

    async def orders(self, ref, symbol):
        self.calls.append("orders")
        if self.fail == "orders":
            raise module().AccountReadUnavailable("rate_limit", retry_after_seconds=90)
        return (map_order(order_wire(), ref),)

    async def trades(self, ref, symbol, cursor):
        self.calls.append("trades")
        if self.fail == "trades":
            raise module().AccountReadUnavailable("transport")
        facts = tuple(map_trade(trade_wire(identity), ref) for identity in self.ids)
        return TradeBatch(
            account_ref=ref,
            symbol=symbol,
            trades=facts,
            next_cursor=TradeCursor(
                last_trade_id=facts[-1].trade_id, last_executed_at=facts[-1].executed_at
            )
            if facts
            else cursor,
            history_complete=True,
        )


@pytest.fixture
def clock():
    return FakeClock(NOW)


@pytest.mark.asyncio
async def test_sync_commits_trades_unclassified_balance_and_cursor_and_honest_cost(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    report = await module().AccountSyncService(port, store, clock).sync("account-1", "BTCUSDT")
    assert report.account.status == "fresh" and report.account.account_revision == 1
    assert report.imported_count == 1 and report.orders[0].order_id == "123"
    assert report.position.quantity == report.account.balances[0].total
    assert report.position.cost_status == "unknown" and report.position.average_cost is None
    assert report.history_complete and not report.cached
    assert (await store.cursor("account-1", "BTCUSDT")).last_trade_id == "1"
    state = await store.load(
        TradeAttribution(account_ref="account-1", symbol="BTCUSDT", trade_id="1").aggregate_id
    )
    assert state.state.original_author == "unclassified" and state.state.executor == "human"


@pytest.mark.asyncio
async def test_poll_interval_serializes_concurrent_sync_and_restart_keeps_facts(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    service = module().AccountSyncService(port, store, clock)
    first, second = await asyncio.gather(
        service.sync("account-1", "BTCUSDT"), service.sync("account-1", "BTCUSDT")
    )
    assert first.cached != second.cached
    assert len(port.calls) == 3
    clock.advance_to(NOW + timedelta(seconds=15))
    refreshed = await service.sync("account-1", "BTCUSDT")
    assert refreshed.imported_count == 0 and refreshed.duplicate_count == 1
    assert refreshed.account.account_revision == 1
    reopened = SqliteObservationStore(store.path)
    await reopened.initialize()
    recovered = (
        await module().AccountSyncService(port, reopened, clock).sync("account-1", "BTCUSDT")
    )
    assert recovered.imported_count == 0 and recovered.account.account_revision == 1


@pytest.mark.asyncio
async def test_balance_drift_is_new_account_revision_not_fabricated_trade(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    service = module().AccountSyncService(port, store, clock)
    await service.sync("account-1", "BTCUSDT")
    port.free = "0.02"
    clock.advance_to(NOW + timedelta(seconds=15))
    report = await service.sync("account-1", "BTCUSDT")
    assert report.account.account_revision == 2 and report.imported_count == 0
    assert len(await store.observed_trades("account-1", "BTCUSDT")) == 1


@pytest.mark.parametrize("failure", ["snapshot", "orders", "trades"])
@pytest.mark.asyncio
async def test_any_read_failure_keeps_confirmed_balance_time_cursor_and_no_partial_facts(
    tmp_path, clock, failure
):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    service = module().AccountSyncService(port, store, clock)
    before = await service.sync("account-1", "BTCUSDT")
    port.free, port.ids, port.fail = "0.02", [1, 2], failure
    clock.advance_to(NOW + timedelta(seconds=15))
    failed = await service.sync("account-1", "BTCUSDT")
    assert failed.account.status == "unavailable"
    assert failed.account.balances == before.account.balances
    assert failed.account.as_of == NOW and failed.account.account_revision == 1
    assert failed.failure_reason is not None and not failed.orders
    assert (await store.cursor("account-1", "BTCUSDT")).last_trade_id == "1"
    assert len(await store.observed_trades("account-1", "BTCUSDT")) == 1
    if failure == "orders":
        clock.advance_to(NOW + timedelta(seconds=31))
        assert (await service.sync("account-1", "BTCUSDT")).cached


@pytest.mark.asyncio
async def test_unconfigured_account_is_unavailable_without_invented_zero_balance(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    port.fail = "snapshot"
    result = await module().AccountSyncService(port, store, clock).sync("account-1", "BTCUSDT")
    assert result.account.status == "unavailable" and result.account.account_revision == 0
    assert not result.account.balances and result.position is None


@pytest.mark.asyncio
async def test_wrong_scope_is_rejected_before_port_call(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    service = module().AccountSyncService(port, store, clock)
    for ref, symbol in (("paper:a", "BTCUSDT"), ("account-1", "ETHUSDT")):
        with pytest.raises(ValueError):
            await service.sync(ref, symbol)
    assert not port.calls


@pytest.mark.asyncio
async def test_report_uses_committed_account_without_a_separate_projection_read(
    tmp_path, clock, monkeypatch
):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()

    async def forbidden(*args):
        pytest.fail("a separate account read can race another writer after commit")

    monkeypatch.setattr(store, "account_snapshot", forbidden)
    result = (
        await module()
        .AccountSyncService(FakeAccount(clock), store, clock)
        .sync("account-1", "BTCUSDT")
    )
    assert result.account.account_revision == 1


@pytest.mark.asyncio
async def test_audit_failure_does_not_publish_or_cache_success_and_retries_cleanly(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_sync BEFORE INSERT ON journal_events "
            "WHEN NEW.kind='trades_imported' "
            "BEGIN SELECT RAISE(ABORT, 'audit failed'); END;"
        )
    port = FakeAccount(clock)
    service = module().AccountSyncService(port, store, clock)
    with pytest.raises(PersistenceUnavailable):
        await service.sync("account-1", "BTCUSDT")
    assert await store.account_snapshot("account-1") is None
    assert not await store.observed_trades("account-1", "BTCUSDT")
    assert await store.cursor("account-1", "BTCUSDT") == TradeCursor()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TRIGGER reject_sync")
    report = await service.sync("account-1", "BTCUSDT")
    assert report.imported_count == 1 and not report.cached


@pytest.mark.asyncio
async def test_cancellation_propagates_without_partial_commit_or_cached_success(tmp_path, clock):
    store = SqliteObservationStore(tmp_path / "sync.sqlite3")
    await store.initialize()
    port = FakeAccount(clock)
    entered = asyncio.Event()

    async def blocked(ref, symbol, cursor):
        entered.set()
        await asyncio.Event().wait()

    port.trades = blocked
    service = module().AccountSyncService(port, store, clock)
    task = asyncio.create_task(service.sync("account-1", "BTCUSDT"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.account_snapshot("account-1") is None
    assert not await store.observed_trades("account-1", "BTCUSDT")
