"""Latest-only queries preserve exact amounts and honest freshness offline."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.account import AccountSnapshot, Balance, PositionView, TradeCursor
from agent_platform.domain.market import BookTicker, MarketSnapshot, TradeTick
from agent_platform.domain.sync import SyncReport
from tests.domain.test_decisions import NOW


def modules():
    return (
        importlib.import_module("agent_platform.domain.overview"),
        importlib.import_module("agent_platform.application.queries"),
        importlib.import_module("agent_platform.runtime.latest"),
    )


def market():
    return MarketSnapshot(
        symbol="BTCUSDT",
        as_of=NOW,
        status="ready",
        latest_received_at=NOW,
        latest_quote_at=NOW,
        book_as_of=NOW,
        book=BookTicker(
            symbol="BTCUSDT", bid="60000.00000001", ask="60001", bid_quantity="1", ask_quantity="2"
        ),
    )


def report(*, status="fresh", revision=1):
    return SyncReport(
        account=AccountSnapshot(
            account_ref="private-account-never-export",
            as_of=NOW,
            status=status,
            account_revision=revision,
            balances=(
                Balance(asset="BTC", free="0.00000001", locked="0.1"),
                Balance(asset="USDT", free="123.123456789123456789", locked="2"),
                Balance(asset="ETH", free="100", locked="0"),
            ),
        ),
        position=PositionView(symbol="BTCUSDT", quantity="0.10000001", cost_status="unknown")
        if revision
        else None,
        attempted_at=NOW,
        next_attempt_at=NOW + timedelta(seconds=15),
        next_cursor=TradeCursor(last_trade_id="private-trade-id", last_executed_at=NOW),
    )


async def service(**kwargs):
    domain, queries, latest = modules()
    clock = FakeClock(NOW)
    cache = latest.LatestOverview(clock, mode="fake", market_source="fake", account_source="fake")
    await cache.publish(
        domain.OverviewFrame(
            mode="fake", market_source="fake", account_source="fake", captured_at=NOW, **kwargs
        )
    )
    return queries.QueryService(cache, clock), cache, clock


@pytest.mark.asyncio
async def test_exact_display_and_account_scope_are_not_exported():
    query, _, _ = await service(market=market(), sync=report())
    view = await query.overview()
    data = view.model_dump(mode="json")
    assert data["market"]["bid"] == "60000.00000001"
    assert data["account"]["balances"][1]["free"] == "123.123456789123456789"
    assert data["account"]["quantity"] == "0.10000001"
    assert data["account"]["cost_status"] == "unknown"
    assert data["account"]["average_cost"] is None
    assert "private-account" not in view.model_dump_json()
    assert "private-trade-id" not in view.model_dump_json()
    assert "ETH" not in view.model_dump_json()
    assert data["mode"] == "fake" and data["market"]["source"] == "fake"


@pytest.mark.asyncio
async def test_missing_observations_remain_null_unavailable_not_hold_or_zero():
    query, _, _ = await service()
    view = await query.overview()
    assert view.market.price is None and view.account.quantity is None
    assert view.account.balances == ()
    assert view.advice_status == "unavailable" and view.jev_status == "unspecified"
    assert not view.paid_models_enabled


@pytest.mark.asyncio
async def test_time_rechecks_cached_data_without_network_refresh():
    query, _, clock = await service(market=market(), sync=report())
    clock.advance_to(NOW + timedelta(seconds=5))
    assert (await query.overview()).market.status == "ready"
    clock.advance_to(NOW + timedelta(seconds=6))
    assert (await query.overview()).market.status == "stale"
    clock.advance_to(NOW + timedelta(seconds=60))
    assert (await query.overview()).account.status == "fresh"
    clock.advance_to(NOW + timedelta(seconds=61))
    assert (await query.overview()).account.status == "stale"
    assert (await query.overview()).account.quantity == report().position.quantity


@pytest.mark.asyncio
async def test_never_confirmed_balance_does_not_become_zero_or_known():
    query, _, _ = await service(sync=report(status="unavailable", revision=0))
    view = await query.overview()
    assert view.account.quantity is None and view.account.balances == ()
    assert view.account.as_of is None


@pytest.mark.asyncio
async def test_market_ready_can_coexist_with_unavailable_account():
    query, _, _ = await service(market=market(), sync=report(status="unavailable"))
    view = await query.overview()
    assert view.market.status == "ready" and view.account.status == "unavailable"
    assert view.account.quantity is not None  # Previously confirmed, explicitly unavailable.


@pytest.mark.parametrize(
    "changes",
    [
        {"captured_at": NOW - timedelta(seconds=1), "market": market()},
        {"mode": "live_read_only", "market_source": "fake"},
        {"mode": "disabled", "market": market()},
    ],
)
def test_frame_rejects_future_or_mixed_origin(changes):
    domain, _, _ = modules()
    payload = dict(mode="fake", market_source="fake", account_source="fake", captured_at=NOW)
    payload.update(changes)
    with pytest.raises(ValueError):
        domain.OverviewFrame(**payload)


@pytest.mark.asyncio
async def test_reconnect_returns_latest_without_replaying_unbounded_history():
    domain, _, _ = modules()
    query, cache, _ = await service(market=market())
    old = (await query.overview()).event_id
    for _ in range(100):
        await cache.publish(
            domain.OverviewFrame(
                mode="fake", market_source="fake", account_source="fake", captured_at=NOW
            )
        )
    stream = query.watch(old)
    newest = await anext(stream)
    assert newest.event_id != old and newest.market.price is None
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not pending.done()
    await cache.publish(
        domain.OverviewFrame(
            mode="fake",
            market_source="fake",
            account_source="fake",
            captured_at=NOW,
            market=market(),
        )
    )
    assert (await pending).market.bid == market().book.bid
    await stream.aclose()


@pytest.mark.asyncio
async def test_cancelled_sse_waiter_does_not_break_other_readers():
    query, _, _ = await service()
    stream = query.watch("old-process-id")
    await anext(stream)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await query.overview()).advice_status == "unavailable"


@pytest.mark.asyncio
async def test_new_trade_does_not_refresh_older_book_status():
    old_book = market().model_copy(
        update={
            "book_as_of": NOW - timedelta(seconds=6),
            "latest_trade": TradeTick(symbol="BTCUSDT", trade_id="1", price="60002", quantity="1"),
        }
    )
    query, _, _ = await service(market=old_book)
    view = await query.overview()
    assert view.market.status == "ready"
    assert view.market.book_status == "stale"
    assert view.market.bid == old_book.book.bid  # Historical value may still be displayed.


@pytest.mark.asyncio
async def test_orders_status_ages_separately_from_still_confirmed_balance():
    synced = report().model_copy(update={"orders_as_of": NOW})
    query, _, clock = await service(sync=synced)
    assert (await query.overview()).account.order_count == 0
    assert (await query.overview()).account.orders_status == "fresh"
    clock.advance_to(NOW + timedelta(seconds=61))
    assert (await query.overview()).account.orders_status == "stale"


@pytest.mark.asyncio
async def test_stalled_sampler_sse_heartbeat_rechecks_stale_data(monkeypatch):
    query, _, clock = await service(market=market(), sync=report())
    stream = query.watch(None)
    await anext(stream)
    clock.advance_to(NOW + timedelta(seconds=61))

    async def no_update(after_id):
        raise TimeoutError

    monkeypatch.setattr(query.source, "wait", no_update)
    heartbeat = await anext(stream)
    assert heartbeat is not None and heartbeat.market.status == "stale"
    assert heartbeat.account.status == "stale"
    await stream.aclose()
