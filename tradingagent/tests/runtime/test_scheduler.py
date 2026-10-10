"""Monotonic coalescing, bounded events and risk alerts without any model call."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.decisions import DecisionEvent
from tests.domain.test_decisions import NOW


def module():
    return importlib.import_module("agent_platform.runtime.scheduler")


def event(identity="one", kind="market_change", *, at=NOW, ttl=60, session="session-1"):
    return DecisionEvent(
        event_id=identity,
        session_id=session,
        kind=kind,
        occurred_at=at,
        expires_at=at + timedelta(seconds=ttl),
        evidence_ids=("features-1",),
    )


def test_quiet_samples_do_not_evaluate_until_300_seconds_and_holding_60_seconds():
    clock = FakeClock(NOW)
    scheduler = module().DecisionScheduler("session-1", clock)
    for seconds in range(1, 300):
        clock.advance_to(NOW + timedelta(seconds=seconds))
        assert scheduler.poll() is None
    clock.advance_to(NOW + timedelta(seconds=300))
    trigger = scheduler.poll()
    assert trigger.kind == "periodic"
    scheduler.complete(trigger.trigger_id)
    scheduler.set_holding(True)
    clock.advance_to(NOW + timedelta(seconds=359))
    assert scheduler.poll() is None
    clock.advance_to(NOW + timedelta(seconds=360))
    assert scheduler.poll().kind == "periodic"


def test_only_one_inflight_and_same_kind_30_seconds_cooldown_coalesces_ids():
    clock = FakeClock(NOW)
    scheduler = module().DecisionScheduler("session-1", clock)
    scheduler.notify(event("first"))
    first = scheduler.poll()
    scheduler.notify(event("second"))
    scheduler.notify(event("third"))
    assert scheduler.poll() is None
    scheduler.complete(first.trigger_id)
    assert scheduler.poll() is None
    clock.advance_to(NOW + timedelta(seconds=30))
    second = scheduler.poll()
    assert second.event_ids == ("second", "third")
    with pytest.raises(ValueError):
        scheduler.complete(first.trigger_id)


def test_expired_queued_events_never_trigger_and_queue_is_bounded():
    clock = FakeClock(NOW)
    scheduler = module().DecisionScheduler("session-1", clock)
    for index in range(300):
        scheduler.notify(event(str(index), ttl=10))
    assert scheduler.pending_count <= 64
    clock.advance_to(NOW + timedelta(seconds=11))
    assert scheduler.poll() is None and scheduler.pending_count == 0


@pytest.mark.asyncio
async def test_risk_alert_does_not_wait_for_inflight_model_or_consume_a_trigger():
    scheduler = module().DecisionScheduler("session-1", FakeClock(NOW))
    scheduler.notify(event())
    trigger = await scheduler.next_trigger()
    risk = event("hard", "hard_risk")
    scheduler.notify(risk)
    assert await scheduler.next_alert() == risk
    assert scheduler.poll() is None
    scheduler.complete(trigger.trigger_id)


def test_wall_clock_rollback_cannot_repeat_a_periodic_or_extend_event_expiry():
    class AdjustableClock:
        def __init__(self):
            self.wall, self.elapsed = NOW, 0.0

        def utcnow(self):
            return self.wall

        def monotonic(self):
            return self.elapsed

    clock = AdjustableClock()
    scheduler = module().DecisionScheduler("session-1", clock)
    scheduler.notify(event(ttl=10))
    clock.wall -= timedelta(hours=1)
    clock.elapsed = 11
    assert scheduler.poll() is None
    clock.elapsed = 300
    trigger = scheduler.poll()
    assert trigger.kind == "periodic" and trigger.requested_at == clock.wall
    scheduler.complete(trigger.trigger_id)
    assert scheduler.poll() is None


def test_wrong_session_or_future_notification_is_rejected_before_queueing():
    scheduler = module().DecisionScheduler("session-1", FakeClock(NOW))
    for notice in (event(session="other"), event(at=NOW + timedelta(seconds=1))):
        with pytest.raises(ValueError):
            scheduler.notify(notice)
    assert scheduler.pending_count == 0


@pytest.mark.asyncio
async def test_wait_cancellation_does_not_create_an_inflight_trigger():
    scheduler = module().DecisionScheduler("session-1", FakeClock(NOW))
    task = asyncio.create_task(scheduler.next_trigger())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler.inflight is None


@pytest.mark.parametrize("retained", ["pending", "alert", "inflight"])
def test_seen_eviction_cannot_allow_changed_retained_event_identity(retained):
    scheduler = module().DecisionScheduler("session-1", FakeClock(NOW))
    original = event("retained", "hard_risk" if retained == "alert" else "market_change", ttl=300)
    scheduler.notify(original)
    if retained == "inflight":
        scheduler.poll()
    for index in range(256):
        scheduler.notify(
            event(f"filler-{index}", "market_change" if retained == "alert" else "hard_risk")
        )
    with pytest.raises(ValueError):
        scheduler.notify(original.model_copy(update={"priority": 99}))


@pytest.mark.parametrize(
    "ticks",
    [(9.999999, 10.000001), (9.9999999, 9.9999999)],
    ids=["crosses-expiry", "sub-microsecond"],
)
def test_near_expiry_member_cannot_discard_a_valid_coalesced_peer(ticks):
    class ScriptClock:
        def __init__(self):
            self.ticks = []

        def utcnow(self):
            return NOW

        def monotonic(self):
            return self.ticks.pop(0) if self.ticks else 0

    clock = ScriptClock()
    scheduler = module().DecisionScheduler("session-1", clock)
    scheduler.notify(event("nearly-expired", ttl=10))
    scheduler.notify(event("valid-peer", ttl=60))
    clock.ticks = list(ticks)
    trigger = scheduler.poll()
    assert trigger.event_ids == ("valid-peer",) and trigger.expires_at > trigger.requested_at
