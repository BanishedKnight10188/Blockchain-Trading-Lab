"""Bounded overlapping inference, serialized funds and stale verdict rejection."""

import asyncio
import json
from datetime import timedelta

import pytest

from agent_platform.runtime.futures_trading import FuturesTradingRuntime
from tests.integration import test_futures_trading_core as core

assembled = core.assembled


@pytest.mark.asyncio
async def test_confirmed_expired_verdict_is_skipped_without_pausing_the_lane(assembled):
    from agent_platform.ports.model import ModelCallFailed

    svc, clock, _, _ = assembled
    run = await core.configured(svc)
    original = svc.model

    class Expired:
        async def decide(self, request):
            reply = await original.decide(request)
            clock.advance_to(request.deadline)
            raise ModelCallFailed("decision_expired", reply.usage)

    svc.model = Expired()
    cycle = await svc.step()
    assert cycle.status == "rejected" and cycle.reason == "decision_expired"
    assert cycle.usage.billing_status == "confirmed"
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.status == "running" and account.quantity == 0


@pytest.mark.asyncio
async def test_real_lane_drops_preparation_burst_instead_of_catching_up(assembled, monkeypatch):
    svc, _, _, _ = assembled
    # Emulate a paid lane with the zero-fee fake port; no external inference.
    await core.configured(svc)
    svc.decision_source = "real_jev"
    # Source binding is independently covered; isolate paid-lane cadence here.
    monkeypatch.setattr(svc, "_sources", lambda run: None)
    first = await svc.step()
    assert first.status == "wait"
    second = await svc.step()
    assert second.status == "rejected" and second.reason == "prediction_tick_skipped"
    assert second.usage is None and svc.model.calls == 1
    await asyncio.sleep(1.02)
    third = await svc.step()
    assert third.status == "wait" and svc.model.calls == 2


@pytest.mark.asyncio
async def test_real_scheduler_rebases_on_actual_dispatch_after_variable_preparation():
    class Store:
        async def acquire_owner(self):
            pass

        async def release_owner(self):
            pass

    class Service:
        store = Store()
        ready = False
        decision_source = "real_jev"
        last_failure = None
        _last_model_dispatch = None
        decision_requested = asyncio.Event()
        starts = []
        two_dispatches = asyncio.Event()

        async def recover(self):
            pass

        async def maintain(self):
            pass

        async def step(self):
            await asyncio.sleep(0.12 if not self.starts else 0.005)
            self._last_model_dispatch = asyncio.get_running_loop().time()
            self.starts.append(self._last_model_dispatch)
            if len(self.starts) == 2:
                self.two_dispatches.set()

    svc = Service()
    worker = FuturesTradingRuntime(svc, decision_seconds=1, maintenance_seconds=1)
    await worker.start()
    try:
        await asyncio.wait_for(svc.two_dispatches.wait(), 3)
        assert svc.starts[1] - svc.starts[0] >= 1
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_choice", ["WAIT", "OPEN_SHORT"])
async def test_newer_verdict_prevents_slow_old_open(assembled, new_choice):
    svc, clock, _, _ = assembled
    run = await core.configured(svc)
    original = svc.model
    original.choices = ("OPEN_LONG", new_choice)
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    class SlowFirst:
        async def decide(self, request):
            requests.append(request)
            response = await original.decide(request)
            if len(requests) == 1:
                started.set()
                await release.wait()
            return response

    svc.model = SlowFirst()
    first = asyncio.create_task(svc.step())
    try:
        await asyncio.wait_for(started.wait(), 2)
        clock.advance_to(clock.utcnow() + timedelta(seconds=1))
        second = await svc.step()
        assert second is not None and second.status == (
            "wait" if new_choice == "WAIT" else "filled"
        )
    finally:
        release.set()
        old = await first
    assert old.reason == ("prediction_superseded" if new_choice == "WAIT" else "account_changed")
    assert old.command_id is None
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.quantity == (0 if new_choice == "WAIT" else 1)
    assert account.free_usdt == (1000 if new_choice == "WAIT" else 599)
    assert len(await svc.store.recent(run.scope.account_ref)) == 2
    state = json.loads(requests[0].state_json)
    assert state["position_management"]["decision_interval_seconds"] == 1
    assert (requests[0].deadline - requests[0].captured_at).total_seconds() == 3


@pytest.mark.asyncio
async def test_fixed_dispatch_cadence_overlaps_slow_predictions_and_bounds_capacity():
    class Store:
        async def acquire_owner(self):
            pass

        async def release_owner(self):
            pass

    class Service:
        store = Store()
        ready = False
        decision_source = "offline_mock"
        last_failure = None
        decision_requested = asyncio.Event()
        release = asyncio.Event()
        starts = []
        active = 0
        peak = 0

        async def recover(self):
            pass

        async def maintain(self):
            pass

        async def step(self):
            self.starts.append(asyncio.get_running_loop().time())
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    svc = Service()
    worker = FuturesTradingRuntime(svc, decision_seconds=0.03, maintenance_seconds=0.03)
    await worker.start()
    try:
        await asyncio.sleep(0.16)
        assert len(svc.starts) == 3 and svc.peak == 3
        assert svc.starts[2] - svc.starts[0] < 0.12
        assert worker.metrics["skipped_capacity"] >= 1
    finally:
        await worker.stop()
    assert svc.active == 0 and worker.metrics["in_flight"] == 0


@pytest.mark.asyncio
async def test_known_future_funding_does_not_hit_rest_each_prediction(assembled, monkeypatch):
    svc, clock, _, _ = assembled
    run = await core.configured(svc)
    calls = []
    original = svc.maintenance.market.settlements

    async def record(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc.maintenance.market, "settlements", record)
    for _ in range(3):
        clock.advance_to(clock.utcnow() + timedelta(seconds=1))
        await svc.maintenance.maintain(run.scope, run.created_at)
    assert calls == []


@pytest.mark.asyncio
async def test_funding_schedule_shortening_keeps_publication_guard(assembled):
    from agent_platform.ports.trading_execution import ExecutionRejected

    svc, clock, _, _ = assembled
    run = await core.configured(svc)
    market = svc.maintenance.market
    due = clock.utcnow() + timedelta(seconds=2)
    market.next_due = due
    await svc.maintenance.maintain(run.scope, run.created_at)
    assert (await svc.store.checkpoint(run.scope.account_ref)).next_due == due
    clock.advance_to(due + timedelta(seconds=1))
    with pytest.raises(ExecutionRejected, match="funding_publication_pending"):
        await svc.maintenance.maintain(run.scope, run.created_at)


@pytest.mark.asyncio
async def test_sqlite_three_predictions_capacity_and_result_barrier_survive_reopen(assembled):
    from agent_platform.adapters.sqlite.trading_runtime import SqliteTradingRuntimeStore
    from agent_platform.domain.trading_runtime import TradingCycle
    from agent_platform.ports.sessions import RevisionConflict

    svc, clock, ctx, _ = assembled
    run = await core.configured(svc)
    account = await svc.backend.account(run.scope, clock.utcnow())
    cycles = [
        TradingCycle(
            request_id=f"capacity-{n}",
            scope=run.scope,
            style_revision=1,
            trader_revision=1,
            account_revision=account.revision,
            created_at=clock.utcnow(),
        )
        for n in range(4)
    ]
    for cycle in cycles[:3]:
        await svc.store.claim(cycle, max_pending=3)
    with pytest.raises(RevisionConflict):
        await svc.store.claim(cycles[3], max_pending=3)
    assert await svc.store.accept_result(cycles[1].request_id)
    reopened = SqliteTradingRuntimeStore(ctx[5])
    assert not await reopened.accept_result(cycles[0].request_id)
    assert await reopened.accept_result(cycles[2].request_id)
