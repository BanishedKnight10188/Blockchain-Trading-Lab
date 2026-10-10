import importlib
from decimal import Decimal

import pytest

from agent_platform.application.prompting import build_prompt
from agent_platform.application.routing import ModelRouter
from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.domain.routing import RoutingPolicy
from tests.adapters.test_sqlite_decisions import snapshot
from tests.application.test_decision_service import Rules, service
from tests.application.test_decision_service import context as _context
from tests.application.test_routing import tier
from tests.domain.test_decisions import NOW

context = _context


@pytest.mark.asyncio
async def test_invalid_provider_advice_does_not_erase_confirmed_cost(context):
    class Provider:
        async def generate(self, request):
            quote = decision.router.quote(request.route, build_prompt(request))
            usage = ModelUsage(
                request_id=request.request_id,
                route_id=request.route.route_id,
                model_version=request.route.model_version,
                input_tokens=100,
                output_tokens=10,
                estimated_cost_usd=quote.estimated_cost_usd,
                actual_cost_usd="0.0004",
                billing_status="confirmed",
                recorded_at=NOW,
            )
            raise importlib.import_module("agent_platform.ports.model").ModelCallFailed(
                "invalid_model_assessment", usage
            )

    decision, _, _ = service(context, model=Provider(), rules=Rules())
    await decision.decide(decision.prepare(snapshot(context[4]), request_id="provider-fee-1"))
    balance = await context[1].budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert balance.spent_usd == Decimal("0.0004")
    assert balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_selected_strong_model_is_rejected_before_reservation(context):
    class Provider:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            raise RuntimeError("must not dispatch")

    provider = Provider()
    decision, _, clock = service(context, model=provider, rules=Rules())
    decision.model_modules = ModelModulesConfig(strong_model={"model_id": "vendor/reviewer-v1"})
    decision.router = ModelRouter(
        RoutingPolicy(routes=(tier(model_version="vendor/reviewer-v1"),), daily_limit_usd="1"),
        clock,
    )
    await decision.decide(decision.prepare(snapshot(context[4]), request_id="strong-no-reserve"))
    balance = await context[1].budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert provider.calls == 0 and balance.hourly_call_count == 0 and balance.reserved_usd == 0
