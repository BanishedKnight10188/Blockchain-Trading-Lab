"""Run identity and unknown sends survive restarts without repeated provider IO."""

from decimal import Decimal
from importlib import import_module

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.budgets import SqliteBudgetStore
from agent_platform.domain.routing import ModelPrice
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.persistence import BudgetExceeded
from tests.fixtures.event_agent_cases import event, grant, lane, request, response
from tests.fixtures.watch_cases import NOW


async def store(path):
    cls = import_module("agent_platform.adapters.sqlite.event_agent").SqliteAgentRunStore
    value = cls(path)
    await value.initialize()
    await value.save_lane(lane(), expected_revision=0)
    return value


@pytest.mark.asyncio
async def test_request_sent_unknown_is_not_reissued(tmp_path):
    runs = await store(tmp_path / "core.sqlite")
    run = await runs.claim(event(), lane(), NOW)
    req = request(run_id=run.run_id)
    await runs.record_turn(run.run_id, req, None, "SENT")
    reopened = await store(tmp_path / "second.sqlite")
    reopened.path = runs.path
    recovered = await reopened.recover("lane-1", NOW)
    assert recovered[0].status == "RECONCILING"
    assert (await reopened.claim(event(), lane(), NOW)).run_id == run.run_id
    with pytest.raises(ValueError):
        await reopened.record_turn(run.run_id, req, None, "SENT")


@pytest.mark.asyncio
async def test_analysis_claim_is_persistent_and_lane_serialized(tmp_path):
    runs = await store(tmp_path / "core.sqlite")
    run = await runs.claim_analysis(lane(), "analysis-1", NOW)
    assert (await runs.claim_analysis(lane(), "analysis-1", NOW)).run_id == run.run_id
    with pytest.raises(ValueError):
        await runs.claim_analysis(lane(), "analysis-2", NOW)
    await runs.finish(run.run_id, response().final, NOW)
    assert (await runs.claim_analysis(lane(), "analysis-2", NOW)).run_id != run.run_id


@pytest.mark.asyncio
async def test_parent_and_lane_budget_caps(tmp_path):
    budgets = SqliteBudgetStore(tmp_path / "core.sqlite")
    await budgets.initialize()
    from agent_platform.domain.costs import BudgetRequest

    def quoted(identity, estimate):
        return BudgetRequest(
            request_id=identity,
            route_id="event-agent",
            purpose="advisory",
            price_version="test-price",
            estimated_cost_usd=estimate,
            daily_limit_usd="0.02",
            requested_at=NOW,
        )

    await budgets.reserve_agent(quoted("one", "0.004"), grant(lane_total_usd="0.005"))
    with pytest.raises(BudgetExceeded):
        await budgets.reserve_agent(quoted("two", "0.002"), grant(lane_total_usd="0.005"))
    with pytest.raises(BudgetExceeded):
        await budgets.reserve_agent(quoted("three", "0.002"), grant(parent_total_usd="0.005"))
    assert (
        await budgets.budget_balance(NOW, daily_limit_usd="0.02", cumulative=True)
    ).reserved_usd == Decimal("0.004")


@pytest.mark.asyncio
async def test_paid_call_requires_new_grant_and_unknown_keeps_reservation(tmp_path):
    budgets = SqliteBudgetStore(tmp_path / "core.sqlite")
    await budgets.initialize()

    class Provider:
        paid = True
        calls = 0

        async def turn(self, request):
            self.calls += 1
            raise ModelCallFailed("provider_error")

    cls = import_module("agent_platform.application.agent_models").BudgetedAgentModel
    provider = Provider()
    price = ModelPrice(
        version="test-price",
        input_usd_per_million="0.000001",
        output_usd_per_million="0.000001",
        verified_at=NOW,
    )
    wrapped = cls(provider, budgets, FakeClock(NOW), price=price, grant=None)
    with pytest.raises(ValueError):
        await wrapped.turn(request())
    assert provider.calls == 0
    wrapped = cls(provider, budgets, FakeClock(NOW), price=price, grant=grant())
    with pytest.raises(ModelCallFailed):
        await wrapped.turn(request())
    with pytest.raises(ValueError):
        await wrapped.turn(request())
    assert provider.calls == 1
    balance = await budgets.budget_balance(NOW, daily_limit_usd="0.02", cumulative=True)
    assert balance.reserved_usd > 0
