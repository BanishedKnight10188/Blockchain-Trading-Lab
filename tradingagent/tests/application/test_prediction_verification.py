"""A diagnostic batch must never dispatch beyond its explicit paid-call ceiling."""

import asyncio
from types import SimpleNamespace

import pytest

from agent_platform.ports.model import ModelCallFailed


@pytest.mark.asyncio
async def test_parallel_batch_never_exceeds_call_limit_and_waits_for_active_calls():
    from agent_platform.application.prediction_verification import BoundedDecisionModel

    release = asyncio.Event()
    calls = []

    class Model:
        enabled = True

        async def decide(self, request):
            calls.append(request.request_id)
            await release.wait()
            return request.request_id

    model = BoundedDecisionModel(Model(), max_calls=3)
    tasks = [
        asyncio.create_task(model.decide(SimpleNamespace(request_id=str(i)))) for i in range(6)
    ]
    await asyncio.sleep(0.02)
    assert model.calls == model.in_flight == 3 and model.limit_reached
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(calls) == 3 and sum(isinstance(r, ModelCallFailed) for r in results) == 3
    assert model.in_flight == 0 and len(model.observations) == 3
    assert all(r["elapsed_ms"] >= 0 for r in model.observations)


@pytest.mark.asyncio
async def test_cancelled_prediction_is_counted_and_never_replaced():
    from agent_platform.application.prediction_verification import BoundedDecisionModel

    ready = asyncio.Event()

    class Model:
        enabled = True

        async def decide(self, request):
            ready.set()
            await asyncio.Event().wait()

    model = BoundedDecisionModel(Model(), max_calls=1)
    task = asyncio.create_task(model.decide(SimpleNamespace(request_id="cancelled")))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert model.calls == 1 and model.in_flight == 0
    assert model.observations[0]["outcome"] == "cancelled"
    with pytest.raises(ModelCallFailed):
        await model.decide(SimpleNamespace(request_id="second"))
