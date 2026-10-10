"""Reservations, hourly allowance and uncertain bills survive concurrency and restart."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest
import pytest_asyncio

from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.ports.persistence import (
    BudgetExceeded,
    BudgetFrozen,
    BudgetStorePort,
    HourlyCallLimitExceeded,
    RequestIdentityConflict,
    SettlementConflict,
)
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.domain.test_cost_contracts import NOW, request_data


@pytest_asyncio.fixture
async def context(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite.budgets")
    path = tmp_path / "agent.sqlite3"
    store = module.SqliteBudgetStore(path)
    await store.initialize()
    return module, store, path


def request(request_id="request-1", **changes):
    return BudgetRequest(**{**request_data(), "request_id": request_id, **changes})


def usage(reservation, cost=None, **changes):
    return ModelUsage(
        **{
            "request_id": reservation.request.request_id,
            "route_id": reservation.request.route_id,
            "model_version": "fake-v1",
            "input_tokens": 10,
            "output_tokens": 20,
            "estimated_cost_usd": reservation.request.estimated_cost_usd,
            "actual_cost_usd": cost,
            "billing_status": "unknown" if cost is None else "confirmed",
            "recorded_at": reservation.request.requested_at + timedelta(seconds=1),
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_two_concurrent_sixty_cent_reservations_cannot_spend_one_dollar(context):
    module, store, path = context
    second_writer = module.SqliteBudgetStore(path)
    await second_writer.initialize()
    assert isinstance(store, BudgetStorePort)
    outcomes = await asyncio.gather(
        store.reserve(request("request-1")),
        second_writer.reserve(request("request-2")),
        return_exceptions=True,
    )
    assert sum(isinstance(item, BudgetExceeded) for item in outcomes) == 1


@pytest.mark.asyncio
async def test_request_id_is_idempotent_and_cannot_change_its_amount(context):
    _, store, _ = context
    first = await store.reserve(request(hourly_call_limit=1))
    assert await store.reserve(request(hourly_call_limit=1)) == first
    with pytest.raises(RequestIdentityConflict):
        await store.reserve(request(estimated_cost_usd="0.4", hourly_call_limit=1))


@pytest.mark.asyncio
async def test_restart_keeps_unknown_exposure_and_hourly_calls(context):
    module, store, path = context
    first = await store.reserve(request(hourly_call_limit=1))
    unresolved = await store.settle(first.reservation_id, usage(first))
    assert unresolved.spent_usd == 0
    assert unresolved.reserved_usd == first.request.estimated_cost_usd
    reopened = module.SqliteBudgetStore(path)
    await reopened.initialize()
    assert (await reopened.reserve(first.request)).status == "unknown"
    with pytest.raises(HourlyCallLimitExceeded):
        await reopened.reserve(request("request-2", estimated_cost_usd="0.1", hourly_call_limit=1))


@pytest.mark.asyncio
async def test_settlement_releases_only_confirmed_cost_and_is_idempotent(context):
    _, store, _ = context
    first = await store.reserve(request())
    confirmed = usage(first, "0.4")
    settled = await store.settle(first.reservation_id, confirmed)
    assert settled.spent_usd == confirmed.actual_cost_usd
    assert settled.reserved_usd == 0
    assert await store.settle(first.reservation_id, confirmed) == settled
    with pytest.raises(SettlementConflict):
        await store.settle(first.reservation_id, usage(first, "0.3"))
    with pytest.raises(SettlementConflict):
        await store.settle(first.reservation_id, usage(first))


@pytest.mark.asyncio
async def test_actual_cost_over_estimate_freezes_calls_across_restart_and_day_change(context):
    module, store, path = context
    first = await store.reserve(request())
    balance = await store.settle(first.reservation_id, usage(first, "0.8"))
    assert balance.spent_usd == usage(first, "0.8").actual_cost_usd
    assert balance.billing_frozen
    reopened = module.SqliteBudgetStore(path)
    await reopened.initialize()
    with pytest.raises(BudgetFrozen):
        await reopened.reserve(request("request-2", requested_at=NOW + timedelta(days=1)))


@pytest.mark.asyncio
async def test_hourly_window_rolls_but_daily_spend_remains(context):
    _, store, _ = context
    first = await store.reserve(request(hourly_call_limit=1))
    await store.settle(first.reservation_id, usage(first, "0.4"))
    later = NOW + timedelta(hours=1, seconds=1)
    next_reservation = await store.reserve(
        request("request-2", estimated_cost_usd="0.1", hourly_call_limit=1, requested_at=later)
    )
    result = await store.settle(next_reservation.reservation_id, usage(next_reservation, "0.1"))
    assert result.spent_usd == usage(first, "0.5").actual_cost_usd
    assert result.hourly_call_count == 1


@pytest.mark.asyncio
async def test_new_budget_day_does_not_forget_global_hourly_allowance(context):
    _, store, _ = context
    before = NOW.replace(hour=15, minute=59)
    await store.reserve(request(hourly_call_limit=1, requested_at=before))
    with pytest.raises(HourlyCallLimitExceeded):
        await store.reserve(
            request("request-2", hourly_call_limit=1, requested_at=before + timedelta(minutes=2))
        )


@pytest.mark.asyncio
async def test_zero_budget_and_wrong_usage_identity_cannot_change_the_ledger(context):
    _, store, _ = context
    with pytest.raises(BudgetExceeded):
        await store.reserve(request(daily_limit_usd="0"))
    first = await store.reserve(request())
    with pytest.raises(SettlementConflict):
        await store.settle(first.reservation_id, usage(first, "0.4", request_id="wrong-request"))
    assert (await store.reserve(first.request)).status == "reserved"


@pytest.mark.asyncio
async def test_usage_cannot_rewrite_the_original_estimated_cost(context):
    _, store, _ = context
    first = await store.reserve(request())
    with pytest.raises(SettlementConflict):
        await store.settle(first.reservation_id, usage(first, "0.4", estimated_cost_usd="0.1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["reservation", "settlement"])
async def test_audit_failure_rolls_back_budget_mutations(context, phase):
    _, store, path = context
    first = await store.reserve(request()) if phase == "settlement" else None
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_budget_audit BEFORE INSERT ON journal_events
            BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END;
        """)
    with pytest.raises(PersistenceUnavailable):
        if first is None:
            await store.reserve(request())
        else:
            await store.settle(first.reservation_id, usage(first, "0.8"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM budget_requests").fetchone()[0] == (
            0 if first is None else 1
        )
        assert connection.execute("SELECT billing_frozen FROM budget_settings").fetchone()[0] == 0
    if first is not None:
        assert (await store.reserve(first.request)).status == "reserved"
