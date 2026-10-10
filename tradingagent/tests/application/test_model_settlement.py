"""Cancellation cannot erase known billing or skip UNKNOWN observations at a locked store."""

import asyncio
from decimal import Decimal

import pytest

from agent_platform.application.prompting import build_prompt
from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.model_calls import ModelResponse
from tests.adapters.test_sqlite_decisions import snapshot
from tests.application.test_decision_models import setup, valid_response
from tests.application.test_decision_service import Rules, service
from tests.application.test_decision_service import context as _context
from tests.domain.test_decisions import NOW

context = _context


@pytest.mark.parametrize("engine", ["advisory", "typed"])
@pytest.mark.parametrize("billing", ["confirmed", "unknown"])
@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_complete_settlement(
    context, tmp_path, engine, billing
):
    entered, settling = asyncio.Event(), asyncio.Event()

    class Port:
        async def reply(self, request, quote):
            if billing == "unknown":
                entered.set()
                await asyncio.Event().wait()
            await store._io_lock.acquire()
            if engine == "typed":
                return valid_response(request, quote)
            return ModelResponse(
                request_id=request.request_id,
                assessment={"action": "hold", "source": "fake", "explanation": "offline"},
                usage=ModelUsage(
                    request_id=request.request_id,
                    route_id=request.route.route_id,
                    model_version=request.route.model_version,
                    input_tokens=100,
                    output_tokens=10,
                    estimated_cost_usd=quote.estimated_cost_usd,
                    actual_cost_usd="0.0000084",
                    billing_status="confirmed",
                    recorded_at=NOW,
                ),
            )

        async def generate(self, request):
            return await self.reply(
                request, worker.router.quote(request.route, build_prompt(request))
            )

        async def decide(self, request, quote):
            return await self.reply(request, quote)

    if engine == "typed":
        worker, store, request = await setup(tmp_path, port=Port())
    else:
        worker, _, _ = service(context, model=Port(), rules=Rules())
        store = context[1]
        request = worker.prepare(snapshot(context[4]), request_id="cancel-settlement")
    original = store.settle

    async def observe_settle(reservation_id, usage):
        settling.set()
        return await original(reservation_id, usage)

    store.settle = observe_settle
    task = asyncio.create_task(worker.decide(request))
    try:
        if billing == "unknown":
            await asyncio.wait_for(entered.wait(), 1)
            await store._io_lock.acquire()
            task.cancel()
        await asyncio.wait_for(settling.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        if store._io_lock.locked():
            store._io_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    page = await store.scan(0, 1000)
    fees = [r.event.payload for r in page.records if r.event.kind == "model_usage_recorded"]
    assert len(fees) == 1 and fees[0].billing_status == billing
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    if billing == "confirmed":
        assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0
    else:
        assert balance.spent_usd == 0 and balance.reserved_usd > 0
