"""Independent lane runtime, gap recovery and explicit stop."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from tests.adapters.test_watch_store import stores
from tests.fixtures.watch_cases import NOW, candles, definition, frame


def service(store, lane="lane-1"):
    cls = importlib.import_module("agent_platform.application.watches").WatchService
    return cls(store, lane_id=lane)


@pytest.mark.asyncio
async def test_gap_recovery_suppresses_old_signal(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    await store.create(definition())
    app = service(store)
    bars = candles()
    gap = frame((*bars[:20], *bars[21:]))
    assert (await app.process(gap, NOW))[0].reason == "data_gap"
    late = NOW + timedelta(seconds=121)
    repaired = frame(received_at=late)
    result = (await app.process(repaired, late))[0]
    assert result.state == "TRIGGERED"
    assert await events.claim("lane-1", late, 120) is None
    assert (await events.delivery(result.event_id)).status == "SUPPRESSED"


@pytest.mark.asyncio
async def test_service_isolates_lanes_and_runtime_stops_only_its_streams(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    await store.create(definition())
    await store.create(definition(watch_id="other", lane_id="lane-2"))
    app = service(store)
    ready, closed = asyncio.Event(), asyncio.Event()

    class Source:
        def __init__(self):
            self.after = []

        async def stream(self, symbol, interval, after):
            self.after.append(after)
            try:
                yield frame()
                ready.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    source = Source()
    cls = importlib.import_module("agent_platform.runtime.watches").WatchRuntime
    runtime = cls(app, source, FakeClock(NOW), poll_seconds=0.01)
    await runtime.start()
    await asyncio.wait_for(ready.wait(), 2)
    await runtime.stop()
    assert closed.is_set() and not runtime.running
    assert (await store.get("other")).state == "ARMED"
    assert await events.claim("lane-1", NOW, 120)
    assert await events.claim("lane-2", NOW, 120) is None


@pytest.mark.asyncio
async def test_runtime_expires_watch_without_any_market_message(tmp_path):
    store, _ = await stores(tmp_path / "core.sqlite")
    await store.create(definition(expires_at=NOW))

    class Source:
        async def stream(self, *args):
            await asyncio.Event().wait()
            yield frame()

    cls = importlib.import_module("agent_platform.runtime.watches").WatchRuntime
    runtime = cls(service(store), Source(), FakeClock(NOW), poll_seconds=0.01)
    await runtime.start()
    for _ in range(100):
        if (await store.get("watch-1")).state == "EXPIRED":
            break
        await asyncio.sleep(0.01)
    await runtime.stop()
    assert (await store.get("watch-1")).state == "EXPIRED"


@pytest.mark.asyncio
async def test_post_trigger_conflict_blocks_queued_event_without_armed_observer(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    await store.create(definition())
    app = service(store)
    await app.process(frame(), NOW)
    changed = frame(tuple(c.model_copy(update={"volume": c.volume + 1}) for c in candles()))
    await app.process(changed, NOW)
    assert await events.claim("lane-1", NOW, 120) is None


@pytest.mark.asyncio
async def test_same_candle_dedup_cannot_override_expiration(tmp_path):
    store, _ = await stores(tmp_path / "core.sqlite")
    await store.create(
        definition(
            expires_at=NOW + timedelta(minutes=1),
            trigger={
                "logic": "ALL",
                "conditions": [
                    {"metric": "candle.close", "op": "GT", "value": "109"},
                ],
            },
        )
    )
    app = service(store)
    await app.process(frame(), NOW)
    late = NOW + timedelta(minutes=1)
    await app.process(frame(received_at=late), late)
    assert (await store.get("watch-1")).state == "EXPIRED"


@pytest.mark.asyncio
async def test_runtime_keeps_pending_partition_until_conflict_observed(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    await store.create(definition())
    send_conflict, seeded, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Source:
        async def stream(self, *args):
            try:
                yield frame()
                seeded.set()
                await send_conflict.wait()
                yield frame(tuple(c.model_copy(update={"volume": c.volume + 1}) for c in candles()))
                await asyncio.Event().wait()
            finally:
                closed.set()

    cls = importlib.import_module("agent_platform.runtime.watches").WatchRuntime
    runtime = cls(service(store), Source(), FakeClock(NOW), poll_seconds=0.01)
    await runtime.start()
    await asyncio.wait_for(seeded.wait(), 2)
    await asyncio.sleep(0.05)
    assert not closed.is_set()
    send_conflict.set()
    for _ in range(100):
        if (await events.delivery((await store.get("watch-1")).event_id)).status == "SUPPRESSED":
            break
        await asyncio.sleep(0.01)
    await runtime.stop()
    assert await events.claim("lane-1", NOW, 120) is None


@pytest.mark.asyncio
async def test_adapter_runtime_restart_repairs_invalidation_on_saved_bar(tmp_path):
    from decimal import Decimal

    from tests.adapters.test_watch_data import Public, adapter

    store, _ = await stores(tmp_path / "core.sqlite")
    await store.create(
        definition(
            invalidation={
                "logic": "ALL",
                "conditions": [
                    {"metric": "candle.close", "op": "GTE", "value": "105"},
                ],
            }
        )
    )
    bars = tuple(c.model_copy(update={"quote_volume": Decimal("10500")}) for c in candles())
    await service(store).process(frame((*bars[:20], *bars[21:])), NOW)
    saved = await store.get("watch-1")
    assert saved.reason == "data_gap"
    repaired = asyncio.Event()
    source = adapter(Public(bars))

    class RestartSource:
        async def stream(self, symbol, interval, after):
            assert after == saved.last_candle_key
            async for snapshot in source.stream(symbol, interval, after):
                yield snapshot
                repaired.set()
                await asyncio.Event().wait()

    cls = importlib.import_module("agent_platform.runtime.watches").WatchRuntime
    runtime = cls(service(store), RestartSource(), FakeClock(NOW), poll_seconds=0.01)
    await runtime.start()
    try:
        await asyncio.wait_for(repaired.wait(), 2)
    finally:
        await runtime.stop()
    assert (await store.get("watch-1")).state == "INVALIDATED"
