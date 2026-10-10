"""Paper transient recovery still owns fee holds and stops a persistent outage."""

from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.sqlite.budgets import SqliteBudgetStore
from agent_platform.application.decision_models import BudgetedDecisionModel
from agent_platform.bootstrap_paper import TrialDecisionModel
from agent_platform.domain.model_diagnostics import ModelDiagnostic
from agent_platform.domain.model_modules import JevModuleSettings
from agent_platform.domain.paper_trials import PaperTrialPolicy
from agent_platform.domain.routing import ModelPrice
from agent_platform.ports.model import ModelCallFailed
from tests.integration.test_futures_trading_core import assembled as assembled
from tests.integration.test_futures_trading_core import configured


async def budgeted(svc, clock, ctx):
    original = svc.model
    store = SqliteBudgetStore(ctx[5].with_name("recovery-budget.sqlite3"))
    await store.initialize()
    price = ModelPrice(
        version=svc.price_version,
        input_usd_per_million="0.042",
        output_usd_per_million="0",
        verified_at=clock.utcnow(),
    )

    class Port:
        requests = []
        failing = True

        async def decide(self, request, quote):
            self.requests.append(request)
            if self.failing:
                raise ModelCallFailed(
                    "provider_timeout", diagnostic=ModelDiagnostic(stage="transport")
                )
            reply = await original.decide(request)
            return reply.model_copy(
                update={
                    "usage": reply.usage.model_copy(
                        update={
                            "estimated_cost_usd": quote.estimated_cost_usd,
                            "actual_cost_usd": Decimal("0.0000084"),
                        }
                    )
                }
            )

    port = Port()
    model = BudgetedDecisionModel(
        port=port,
        budgets=store,
        clock=clock,
        price=price,
        daily_limit_usd="1",
        max_single_cost_usd="0.02",
        settings=JevModuleSettings(enabled=True),
    )
    svc.model = TrialDecisionModel(
        model,
        PaperTrialPolicy(
            trial_total_usd="1",
            single_call_usd="0.02",
            issued_at=clock.utcnow(),
            expires_at=None,
            price=price,
        ),
        clock,
    )
    return store, port


@pytest.mark.asyncio
async def test_paper_timeout_cools_down_then_uses_a_fresh_request_and_keeps_fee_hold(assembled):
    svc, clock, ctx, _ = assembled
    run = await configured(svc)
    store, port = await budgeted(svc, clock, ctx)
    first = await svc.step()
    assert first.reason == "provider_timeout" and first.usage.billing_status == "unknown"
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "running"
    assert svc.model.enabled
    assert await svc.step() is None
    assert len(port.requests) == 1
    held = (await store.budget_balance(clock.utcnow(), daily_limit_usd="1")).reserved_usd
    clock.advance_to(clock.utcnow() + timedelta(seconds=6))
    port.failing = False
    second = await svc.step()
    assert second.status == "wait"
    assert second.request_id != first.request_id
    assert second.model_request.captured_at > first.model_request.captured_at
    assert second.model_request.deadline - second.model_request.captured_at == timedelta(seconds=3)
    assert second.model_request.response_deadline - second.model_request.captured_at == timedelta(
        seconds=10
    )
    assert len(port.requests) == 2
    balance = await store.budget_balance(clock.utcnow(), daily_limit_usd="1")
    assert balance.reserved_usd == held and balance.spent_usd == Decimal("0.0000084")
    assert (await svc.public_view())["model_recovery"]["consecutive_timeouts"] == 0
    assert len(await svc.store.recent(run.scope.account_ref)) == 2


@pytest.mark.asyncio
async def test_three_consecutive_paper_timeouts_pause_without_replaying_requests(assembled):
    svc, clock, ctx, _ = assembled
    run = await configured(svc)
    store, port = await budgeted(svc, clock, ctx)
    for index in range(3):
        cycle = await svc.step()
        assert cycle.reason == "provider_timeout"
        account = await svc.backend.account(run.scope, clock.utcnow())
        assert account.status == ("paused" if index == 2 else "running")
        clock.advance_to(clock.utcnow() + timedelta(seconds=6))
    assert await svc.step() is None
    assert len({r.request_id for r in port.requests}) == 3
    balance = await store.budget_balance(clock.utcnow(), daily_limit_usd="1")
    assert balance.hourly_call_count == 3 and balance.reserved_usd > 0
    assert balance.spent_usd == 0 and account.quantity == 0
    assert not svc.model.enabled
