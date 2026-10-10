"""Read-only background tasks run without browser polling and stop cleanly."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.fake.market import FakeMarket
from agent_platform.domain.market import BookTicker, MarketEvent
from agent_platform.domain.sync import SyncFailureReason
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.application.test_queries import NOW, report


def classes():
    runtime = importlib.import_module("agent_platform.runtime.read_only").ReadOnlyRuntime
    cache = importlib.import_module("agent_platform.runtime.latest").LatestOverview
    return runtime, cache


def observation(*, source="fake"):
    return MarketEvent(
        event_id="book:1",
        symbol="BTCUSDT",
        source=source,
        occurred_at=NOW,
        received_at=NOW,
        time_quality="received",
        payload=BookTicker(
            symbol="BTCUSDT", bid="60000", ask="60001", bid_quantity="1", ask_quantity="2"
        ),
    )


@pytest.mark.asyncio
async def test_same_runtime_restart_does_not_publish_cached_prior_sync_before_new_attempt():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", account_source="fake")

    class Sync:
        async def sync(self, ref, symbol):
            return report().model_copy(update={"cached": self.cached})

        cached = False

    sync = Sync()
    worker = Runtime(cache, clock, account_sync=sync, account_ref="account-1")
    await worker.start()
    await ready(cache, lambda value: value.sync is not None)
    await worker.stop()
    sync.cached = True
    await worker.start()
    try:
        await worker.sample_once()
        assert (await cache.latest()).sync is None
        sync.cached = False
        worker.request_sync()
        await ready(cache, lambda value: value.sync is not None)
        assert not (await cache.latest()).sync.cached
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_immediate_stream_factory_failure_sets_fixed_health_error():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", market_source="fake")

    class BrokenMarket:
        def stream(self, symbols):
            raise RuntimeError("private-url-secret")

        async def latest(self, symbol):
            raise ValueError("no snapshot")

    worker = Runtime(cache, clock, market=BrokenMarket())
    await worker.start()
    try:
        await ready(cache, lambda value: value.market_error is not None)
        assert worker.last_failure == "transport"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_restart_waits_only_remaining_cached_sync_interval(monkeypatch):
    Runtime, Cache = classes()
    clock = FakeClock(NOW + timedelta(seconds=14))
    cache = Cache(clock, mode="fake", account_source="fake")
    waiting, release = asyncio.Event(), asyncio.Event()
    delays = []

    class Sync:
        async def sync(self, ref, symbol):
            return report().model_copy(update={"cached": True})

    async def wait(awaitable, *, timeout):  # noqa: ASYNC109 - exact asyncio.wait_for interface
        awaitable.close()
        delays.append(timeout)
        waiting.set()
        await release.wait()

    monkeypatch.setattr("agent_platform.runtime.read_only.asyncio.wait_for", wait)
    worker = Runtime(cache, clock, account_sync=Sync(), account_ref="account-1")
    await worker.start()
    try:
        await waiting.wait()
        assert delays == [1], "the original gate is due in one second"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_authoritative_sync_failure_is_included_in_safe_worker_health():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", account_source="fake")
    unavailable = report(status="unavailable").model_copy(
        update={"failure_reason": SyncFailureReason.AUTHENTICATION}
    )
    unavailable = type(unavailable).model_validate(
        {
            **unavailable.model_dump(),
            "failure_reason": "authentication",
        }
    )

    class Sync:
        async def sync(self, ref, symbol):
            return unavailable

    worker = Runtime(cache, clock, account_sync=Sync(), account_ref="account-1")
    await worker.start()
    try:
        await ready(cache, lambda value: value.sync is not None)
        assert worker.last_failure == "authentication"
    finally:
        await worker.stop()


async def ready(cache, predicate):
    async with asyncio.timeout(2):
        frame = await cache.latest()
        while not predicate(frame):
            frame = await cache.wait(frame.event_id)


@pytest.mark.asyncio
async def test_market_runs_without_browser_and_has_one_consumer():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", market_source="fake")
    market = FakeMarket((observation(),))
    runtime = Runtime(cache, clock, market=market)
    await runtime.start()
    with pytest.raises(RuntimeError):
        await runtime.start()
    try:
        await ready(cache, lambda frame: frame.market is not None)
        assert (await cache.latest()).market.book.bid == 60000
    finally:
        await runtime.stop()
    assert not runtime.running
    await runtime.stop()


@pytest.mark.asyncio
async def test_sync_failure_does_not_interrupt_public_market():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", market_source="fake", account_source="fake")

    class Sync:
        async def sync(self, ref, symbol):
            return report(status="unavailable")

    runtime = Runtime(
        cache,
        clock,
        market=FakeMarket((observation(),)),
        account_sync=Sync(),
        account_ref="private-account-never-export",
    )
    await runtime.start()
    try:
        await ready(cache, lambda frame: frame.market is not None and frame.sync is not None)
        assert (await cache.latest()).market.status == "ready"
        assert (await cache.latest()).sync.account.status == "unavailable"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_persistence_failure_stops_account_writes_and_is_sanitized():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", account_source="fake")

    class Sync:
        calls = 0

        async def sync(self, ref, symbol):
            self.calls += 1
            raise PersistenceUnavailable("private-secret-never-echo")

    sync = Sync()
    runtime = Runtime(cache, clock, account_sync=sync, account_ref="account-1")
    await runtime.start()
    try:
        await ready(cache, lambda frame: frame.account_error == "persistence")
        await runtime.sample_once()
        await runtime.sample_once()
        assert sync.calls == 1
        assert "private-secret" not in (await cache.latest()).model_dump_json()
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_invalid_origin_does_not_get_labeled_as_live():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="live_read_only", market_source="binance_direct")
    runtime = Runtime(cache, clock, market=FakeMarket((observation(),)))
    await runtime.start()
    try:
        await ready(cache, lambda frame: frame.market_error == "invalid_data")
        assert (await cache.latest()).market is None
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_cancel_stops_pending_network_and_account_tasks():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", market_source="fake", account_source="fake")
    closed = asyncio.Event()

    class Market(FakeMarket):
        async def stream(self, symbols):
            try:
                await asyncio.Event().wait()
                yield observation()
            finally:
                closed.set()

    class Sync:
        cancelled = False

        async def sync(self, ref, symbol):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    sync = Sync()
    runtime = Runtime(cache, clock, market=Market(), account_sync=sync, account_ref="account-1")
    await runtime.start()
    await asyncio.sleep(0)
    await runtime.stop()
    assert closed.is_set() and sync.cancelled


@pytest.mark.asyncio
async def test_disabled_runtime_has_no_network_workers():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock)
    runtime = Runtime(cache, clock)
    await runtime.start()
    try:
        assert (await cache.latest()).mode == "disabled"
        assert runtime.worker_count == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_side", ["market", "account"])
async def test_future_data_failure_is_isolated_to_its_provider(bad_side):
    from tests.application.test_queries import market

    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="fake", market_source="fake", account_source="fake")

    class Market(FakeMarket):
        async def latest(self, symbol):
            value = market()
            return (
                value.model_copy(update={"as_of": NOW + timedelta(seconds=1)})
                if bad_side == "market"
                else value
            )

    class Sync:
        pass

    runtime = Runtime(cache, clock, market=Market(), account_sync=Sync(), account_ref="account-1")
    runtime._sync = (
        report().model_copy(update={"attempted_at": NOW + timedelta(seconds=1)})
        if bad_side == "account"
        else report()
    )
    await runtime.sample_once()
    frame = await cache.latest()
    if bad_side == "market":
        assert frame.market is None and frame.market_error == "invalid_data"
        assert frame.sync is not None and frame.account_error is None
    else:
        assert frame.sync is None and frame.account_error == "invalid_data"
        assert frame.market is not None and frame.market_error is None


@pytest.mark.asyncio
async def test_wrong_source_explicitly_closes_retained_provider_iterator():
    Runtime, Cache = classes()
    clock = FakeClock(NOW)
    cache = Cache(clock, mode="live_read_only", market_source="binance_direct")
    closed = asyncio.Event()

    class Market(FakeMarket):
        def stream(self, symbols):
            async def events():
                try:
                    yield observation()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

            self.retained = events()
            return self.retained

    runtime = Runtime(cache, clock, market=Market())
    await runtime.start()
    try:
        await ready(cache, lambda frame: frame.market_error == "invalid_data")
        assert closed.is_set()
    finally:
        await runtime.stop()
