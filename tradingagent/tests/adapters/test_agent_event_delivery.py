"""Lease ownership, TTL, bounded retries and paid-dispatch recovery."""

from datetime import timedelta

import pytest

from agent_platform.domain.watch_rules import evaluate_watch
from tests.adapters.test_watch_store import stores
from tests.fixtures.watch_cases import NOW, definition, frame


async def queued(path):
    store, events = await stores(path)
    watch = await store.create(definition())
    await store.commit_evaluation("watch-1", 1, frame(), evaluate_watch(watch, frame(), NOW))
    return store, events


@pytest.mark.asyncio
async def test_stale_lease_cannot_ack_or_renew(tmp_path):
    _, events = await queued(tmp_path / "core.sqlite")
    old = await events.claim("lane-1", NOW, 10)
    later = NOW + timedelta(seconds=11)
    new = await events.claim("lane-1", later, 10)
    assert old.lease_token != new.lease_token
    with pytest.raises(ValueError):
        await events.complete(old, "run-old", later)
    with pytest.raises(ValueError):
        await events.renew(old, later, 120)
    renewed = await events.renew(new, later, 30)
    await events.complete(renewed, "run-new", later)
    assert (await events.delivery(new.event.event_id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_ttl_boundary_and_cancellation_suppress(tmp_path):
    store, events = await queued(tmp_path / "core.sqlite")
    assert await events.claim("lane-1", NOW + timedelta(seconds=120), 120) is None
    assert (await events.delivery((await store.get("watch-1")).event_id)).status == "SUPPRESSED"
    store, events = await queued(tmp_path / "other.sqlite")
    watch = await store.get("watch-1")
    await store.cancel("watch-1", watch.revision, NOW)
    assert await events.claim("lane-1", NOW, 120) is None


@pytest.mark.asyncio
async def test_three_attempts_then_failed(tmp_path):
    _, events = await queued(tmp_path / "core.sqlite")
    for attempt in range(1, 4):
        lease = await events.claim("lane-1", NOW, 10)
        delivery = await events.fail(lease, "offline failure", True, NOW)
        assert delivery.attempts == attempt
    assert delivery.status == "FAILED"
    assert await events.claim("lane-1", NOW, 10) is None


@pytest.mark.asyncio
async def test_dispatched_lease_reconciles_without_retry(tmp_path):
    _, events = await queued(tmp_path / "core.sqlite")
    lease = await events.claim("lane-1", NOW, 10)
    await events.mark_dispatched(lease, "run-1", NOW)
    states = await events.recover(NOW + timedelta(seconds=11))
    assert states[0].status == "RECONCILING" and states[0].run_id == "run-1"
    assert await events.claim("lane-1", NOW + timedelta(seconds=11), 10) is None


@pytest.mark.asyncio
async def test_partition_conflict_retires_other_queued_signals(tmp_path):
    store, events = await queued(tmp_path / "core.sqlite")
    watch = await store.create(
        definition(
            watch_id="observer",
            trigger={
                "logic": "ALL",
                "conditions": [{"metric": "candle.close", "op": "GT", "value": "109"}],
            },
        )
    )
    changed = frame(tuple(c.model_copy(update={"volume": c.volume + 1}) for c in frame().candles))
    await store.commit_evaluation("observer", 1, changed, evaluate_watch(watch, changed, NOW))
    assert await events.claim("lane-1", NOW, 120) is None


@pytest.mark.asyncio
async def test_dispatch_after_ttl_or_cancel_cannot_begin(tmp_path):
    store, events = await queued(tmp_path / "core.sqlite")
    lease = await events.claim("lane-1", NOW, 120)
    renewed = await events.renew(lease, NOW + timedelta(seconds=119), 120)
    with pytest.raises(ValueError):
        await events.mark_dispatched(renewed, "late-run", NOW + timedelta(seconds=121))
    watch = await store.get("watch-1")
    await store.cancel("watch-1", watch.revision, NOW)
    with pytest.raises(ValueError):
        await events.mark_dispatched(renewed, "cancelled-run", NOW + timedelta(seconds=119))
