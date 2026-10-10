import asyncio
import importlib
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.budgets import SqliteBudgetStore
from agent_platform.domain.routing import ModelPrice
from agent_platform.ports.persistence import BudgetExceeded, DispatchAlreadyReserved
from tests.application.test_routing import price
from tests.domain.test_decision_models import request_data
from tests.domain.test_decisions import NOW


def valid_response(request, quote, *, at=NOW):
    from agent_platform.domain.costs import ModelUsage
    from agent_platform.domain.decision_models import DecisionModelResponse

    return DecisionModelResponse(
        request_id=request.request_id,
        question_set_version=request.question_set_version,
        answers=(
            {
                "question_id": "gate",
                "kind": "choice",
                "choice": "PASS",
                "confidence": "0.8",
                "probabilities": (
                    {"key": "PASS", "probability": "0.8"},
                    {"key": "REVIEW", "probability": "0.1"},
                    {"key": "ABSTAIN", "probability": "0.1"},
                ),
            },
            {
                "question_id": "quality",
                "kind": "score",
                "score": "1.9",
                "confidence": "0.9",
                "probabilities": (
                    {"key": "0", "probability": "0"},
                    {"key": "1", "probability": "0.1"},
                    {"key": "2", "probability": "0.9"},
                ),
            },
            {"question_id": "conflict", "kind": "noul", "noul": "0.1"},
        ),
        usage=ModelUsage(
            request_id=request.request_id,
            route_id=request.route.route_id,
            model_version=request.route.model_version,
            input_tokens=100,
            output_tokens=10,
            estimated_cost_usd=quote.estimated_cost_usd,
            actual_cost_usd="0.0000084",
            billing_status="confirmed",
            recorded_at=at,
        ),
        provider_metadata={
            "model_id": "typesafe/jev-1.13-20260917",
            "request_id": "gen-offline-1",
            "provider": "TypeSafe",
        },
    )


async def setup(tmp_path, *, daily="1", port=None):
    models = importlib.import_module("agent_platform.domain.decision_models")
    application = importlib.import_module("agent_platform.application.decision_models")
    store = SqliteBudgetStore(tmp_path / "agent.sqlite3")
    await store.initialize()
    table = ModelPrice(
        **{
            **price(),
            "version": "jev-price-v1",
            "input_usd_per_million": "0.042",
            "output_usd_per_million": "0",
        }
    )
    worker = application.BudgetedDecisionModel(
        port=port,
        budgets=store,
        clock=FakeClock(NOW),
        price=table,
        daily_limit_usd=daily,
        max_single_cost_usd="0.1",
        settings=importlib.import_module("agent_platform.domain.model_modules").JevModuleSettings(
            enabled=True
        ),
    )
    return worker, store, models.DecisionModelRequest(**request_data())


@pytest.mark.asyncio
async def test_confirmed_valid_response_expiring_during_settlement_is_not_malformed(tmp_path):
    from agent_platform.ports.model import ModelCallFailed

    class Port:
        async def decide(self, request, quote):
            return valid_response(request, quote)

    worker, store, request = await setup(tmp_path, port=Port())
    settle = store.settle

    async def slow_settlement(*args, **kwargs):
        result = await settle(*args, **kwargs)
        worker.clock.advance_to(request.deadline)
        return result

    store.settle = slow_settlement
    with pytest.raises(ModelCallFailed) as error:
        await worker.decide(request)
    assert error.value.reason == "decision_expired"
    assert error.value.usage.billing_status == "confirmed"
    assert error.value.usage.actual_cost_usd == Decimal("0.0000084")
    assert worker.enabled
    balance = await store.budget_balance(worker.clock.utcnow(), daily_limit_usd=Decimal("1"))
    assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_zero_budget_does_not_dispatch(tmp_path):
    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1

    port = Port()
    worker, store, request = await setup(tmp_path, daily="0", port=port)
    with pytest.raises((BudgetExceeded, ValueError)):
        await worker.decide(request)
    assert port.calls == 0
    assert (await store.budget_balance(NOW, daily_limit_usd=Decimal("0"))).hourly_call_count == 0


@pytest.mark.asyncio
async def test_provider_deadline_without_transport_timeout_is_classified_and_settled(tmp_path):
    from datetime import timedelta

    from agent_platform.ports.model import ModelCallFailed

    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            await asyncio.Event().wait()

    port = Port()
    worker, store, request = await setup(tmp_path, port=port)
    request = request.model_copy(update={"deadline": NOW + timedelta(milliseconds=80)})
    with pytest.raises(ModelCallFailed) as failed:
        await asyncio.wait_for(worker.decide(request), 1)
    assert failed.value.reason == "provider_timeout"
    assert failed.value.diagnostic.stage == "transport"
    assert failed.value.usage.billing_status == "unknown"
    assert port.calls == 1
    balance = await store.budget_balance(NOW, daily_limit_usd="1")
    assert balance.spent_usd == 0 and balance.reserved_usd > 0


@pytest.mark.asyncio
async def test_late_response_wait_is_independent_and_retains_confirmed_fees(tmp_path):
    from datetime import timedelta

    import httpx

    from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
    from agent_platform.adapters.openrouter.transport import OpenRouterClient, OpenRouterCredentials
    from agent_platform.ports.model import ModelCallFailed
    from tests.adapters.test_openrouter_jev import response

    worker, store, request = await setup(tmp_path)
    request = request.model_copy(
        update={
            "deadline": NOW + timedelta(milliseconds=80),
            "response_deadline": NOW + timedelta(seconds=1),
        }
    )
    calls = []

    async def handler(wire):
        calls.append(wire)
        await asyncio.sleep(0.18)
        worker.clock.advance_to(NOW + timedelta(milliseconds=200))
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(
            worker.clock,
            enabled=True,
            client=http,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-late-response"),
        )
        worker.port = OpenRouterDecisionModel(client, worker.clock)
        with pytest.raises(ModelCallFailed) as failed:
            await worker.decide(request)
    assert failed.value.reason == "decision_expired"
    assert failed.value.usage.billing_status == "confirmed"
    assert failed.value.diagnostic.transport_evidence is not None
    assert len(calls) == 1 and worker.enabled
    balance = await store.budget_balance(worker.clock.utcnow(), daily_limit_usd="1")
    assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_owned_transport_timeout_diagnostic_survives_slow_cleanup(tmp_path):
    from datetime import timedelta

    from agent_platform.domain.model_diagnostics import ModelDiagnostic, ModelTransportEvidence
    from agent_platform.ports.model import ModelCallFailed

    class Port:
        provider_deadline_managed = True

        async def decide(self, request, quote):
            # Transport times out, then finishes closing its socket.
            await asyncio.sleep(0.18)
            raise ModelCallFailed(
                "provider_timeout",
                diagnostic=ModelDiagnostic(
                    stage="transport",
                    transport_evidence=ModelTransportEvidence(
                        attempts=1,
                        pre_request_retries=0,
                        request_started=True,
                        connect_ms=0,
                        proxy_connect_ms=0,
                        tls_ms=0,
                        send_ms=0,
                        response_wait_ms=80,
                        body_ms=0,
                        total_ms=180,
                        failed_phase="response_wait",
                        error_type="DeadlineTimeout",
                    ),
                ),
            )

    worker, store, request = await setup(tmp_path, port=Port())
    request = request.model_copy(update={"deadline": NOW + timedelta(milliseconds=80)})
    with pytest.raises(ModelCallFailed) as failed:
        await worker.decide(request)
    assert failed.value.diagnostic.transport_evidence.failed_phase == "response_wait"
    assert failed.value.usage.billing_status == "unknown"
    balance = await store.budget_balance(NOW, daily_limit_usd="1")
    assert balance.spent_usd == 0 and balance.reserved_usd > 0


@pytest.mark.asyncio
async def test_fee_phases_are_recorded_per_request_after_durable_settlement(tmp_path):
    class Port:
        async def decide(self, request, quote):
            await asyncio.sleep(0.006)
            return valid_response(request, quote)

    worker, store, request = await setup(tmp_path, port=Port())
    reserve, settle = store.reserve, store.settle

    async def slow_reserve(*args, **kwargs):
        await asyncio.sleep(0.01)
        return await reserve(*args, **kwargs)

    async def slow_settle(*args, **kwargs):
        await asyncio.sleep(0.015)
        return await settle(*args, **kwargs)

    store.reserve, store.settle = slow_reserve, slow_settle
    response = await worker.decide(request)
    timing = response.local_timing
    assert timing.reserve_us >= 8000 and timing.settle_us >= 12000
    assert timing.provider_us >= 4000
    assert timing.total_us >= sum(
        (
            timing.validation_us,
            timing.reserve_us,
            timing.provider_us,
            timing.fee_validation_us,
            timing.settle_us,
            timing.binding_us,
        )
    )
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert balance.spent_usd == response.usage.actual_cost_usd and balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_transport_failure_keeps_fee_hold_and_independent_phase_evidence(tmp_path):
    from agent_platform.domain.model_diagnostics import ModelDiagnostic
    from agent_platform.ports.model import ModelCallFailed

    class Port:
        async def decide(self, request, quote):
            await asyncio.sleep(0.006)
            raise ModelCallFailed(
                "provider_transport_error", diagnostic=ModelDiagnostic(stage="transport")
            )

    worker, store, request = await setup(tmp_path, port=Port())
    with pytest.raises(ModelCallFailed) as failed:
        await worker.decide(request)
    assert failed.value.diagnostic.stage == "transport"
    assert failed.value.diagnostic.local_timing.provider_us >= 4000
    assert failed.value.diagnostic.local_timing.settle_us > 0
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert (
        balance.spent_usd == 0 and balance.reserved_usd == worker.quote(request).estimated_cost_usd
    )


@pytest.mark.asyncio
async def test_unknown_failure_retains_reservation_and_cannot_redispatch(tmp_path):
    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            raise RuntimeError("untrusted provider body")

    port = Port()
    worker, store, request = await setup(tmp_path, port=port)
    with pytest.raises(importlib.import_module("agent_platform.ports.model").ModelCallFailed):
        await worker.decide(request)
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert balance.reserved_usd > 0 and balance.spent_usd == 0
    with pytest.raises(DispatchAlreadyReserved):
        await worker.decide(request)
    assert port.calls == 1


@pytest.mark.asyncio
async def test_cancellation_keeps_cost_and_hourly_attempt(tmp_path):
    entered = asyncio.Event()

    class Port:
        async def decide(self, request, quote):
            entered.set()
            await asyncio.Event().wait()

    worker, store, request = await setup(tmp_path, port=Port())
    task = asyncio.create_task(worker.decide(request))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert balance.reserved_usd > 0 and balance.hourly_call_count == 1


@pytest.mark.parametrize("case", ["success", "invalid", "late"])
@pytest.mark.asyncio
async def test_sqlite_mock_http_inference_retains_confirmed_fees(tmp_path, case):
    from datetime import timedelta

    import httpx

    from agent_platform.adapters.openrouter.jev import OpenRouterDecisionModel
    from agent_platform.adapters.openrouter.transport import OpenRouterClient, OpenRouterCredentials
    from agent_platform.ports.model import ModelCallFailed
    from tests.adapters.test_openrouter_jev import response

    worker, store, request = await setup(tmp_path)
    value = response()
    if case == "invalid":
        value["answers"]["gate"]["choice"] = "UNKNOWN"

    def handler(r):
        if case == "late":
            worker.clock.advance_to(NOW + timedelta(seconds=20))
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(
            worker.clock,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-integrated-key"),
            enabled=True,
            client=http,
        )
        worker.port = OpenRouterDecisionModel(client, worker.clock)
        if case == "success":
            reply = await worker.decide(request)
            assert reply.answers[0].choice == "PASS"
            assert reply.provider_metadata.model_id == "typesafe/jev-1.13-20260917"
        else:
            with pytest.raises(ModelCallFailed) as error:
                await worker.decide(request)
            assert error.value.usage.actual_cost_usd == Decimal("0.0000084")
    balance = await store.budget_balance(worker.clock.utcnow(), daily_limit_usd=Decimal("1"))
    assert balance.spent_usd == Decimal("0.0000084")
    assert balance.reserved_usd == 0 and balance.hourly_call_count == 1


@pytest.mark.asyncio
async def test_concurrent_typed_requests_share_durable_budget(tmp_path):
    entered = asyncio.Event()

    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    port = Port()
    worker, store, request = await setup(tmp_path, port=port)
    worker.daily_limit_usd = worker.quote(request).estimated_cost_usd
    first = asyncio.create_task(worker.decide(request))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        with pytest.raises(BudgetExceeded):
            await worker.decide(request.model_copy(update={"request_id": "jev-2"}))
        assert port.calls == 1
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


@pytest.mark.asyncio
async def test_typed_fee_rejects_unbounded_or_bypassed_values(tmp_path):
    from agent_platform.domain.costs import ModelUsage

    worker, store, request = await setup(tmp_path)
    quote = worker.quote(request)
    usage = ModelUsage(
        request_id=request.request_id,
        route_id=request.route.route_id,
        model_version=request.route.model_version,
        input_tokens=1,
        output_tokens=1,
        estimated_cost_usd=quote.estimated_cost_usd,
        actual_cost_usd="1e1000000",
        billing_status="confirmed",
        recorded_at=NOW,
    )
    with pytest.raises(ValueError):
        worker._validate_fee(request, usage, quote)
    usage = usage.model_copy(update={"actual_cost_usd": Decimal("0"), "input_tokens": -1})
    with pytest.raises(ValueError):
        worker._validate_fee(request, usage, quote)


@pytest.mark.parametrize("case", ["answer", "model", "price"])
@pytest.mark.asyncio
async def test_response_boundary_rejects_bypassed_answers_models_and_expired_price(tmp_path, case):
    from datetime import timedelta

    from agent_platform.ports.model import ModelCallFailed

    class Port:
        async def decide(self, request, quote):
            reply = valid_response(request, quote)
            if case == "answer":
                return reply.model_copy(
                    update={
                        "answers": (
                            reply.answers[0].model_copy(
                                update={"choice": "BOGUS", "confidence": Decimal("9")}
                            ),
                            *reply.answers[1:],
                        )
                    }
                )
            if case == "model":
                return reply.model_copy(
                    update={
                        "provider_metadata": reply.provider_metadata.model_copy(
                            update={"model_id": "vendor/wrong"}
                        )
                    }
                )
            worker.clock.advance_to(NOW + timedelta(seconds=2))
            return reply.model_copy(
                update={
                    "usage": reply.usage.model_copy(update={"recorded_at": worker.clock.utcnow()})
                }
            )

    worker, store, request = await setup(tmp_path, port=Port())
    if case == "price":
        worker.price = worker.price.model_copy(update={"valid_until": NOW + timedelta(seconds=1)})
    with pytest.raises(ModelCallFailed) as error:
        await worker.decide(request)
    assert error.value.usage.actual_cost_usd == Decimal("0.0000084")
    balance = await store.budget_balance(worker.clock.utcnow(), daily_limit_usd=Decimal("1"))
    assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_default_disabled_jev_never_reserves_or_dispatches_with_a_port(tmp_path):
    from agent_platform.ports.model import ModelCallFailed

    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            return valid_response(request, quote)

    port = Port()
    worker, store, request = await setup(tmp_path, port=port)
    # Test the constructor default, independent of the explicit opt-in fixture used by other tests.
    worker = type(worker)(
        port=port,
        budgets=store,
        clock=worker.clock,
        price=worker.price,
        daily_limit_usd="1",
        max_single_cost_usd="0.1",
    )
    with pytest.raises(ModelCallFailed, match="model_module_disabled"):
        await worker.decide(request)
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert port.calls == 0
    assert balance.hourly_call_count == 0 and balance.reserved_usd == balance.spent_usd == 0
    assert (await store.scan(0, 1000)).records == ()


@pytest.mark.asyncio
async def test_disabling_jev_while_reserve_waits_records_zero_without_dispatch(tmp_path):
    from agent_platform.ports.model import ModelCallFailed

    entered, release = asyncio.Event(), asyncio.Event()

    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            return valid_response(request, quote)

    port = Port()
    worker, store, request = await setup(tmp_path, port=port)
    original = store.reserve

    async def delayed_reserve(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    store.reserve = delayed_reserve
    task = asyncio.create_task(worker.decide(request))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        worker.set_enabled(False)
        release.set()
        with pytest.raises(ModelCallFailed, match="model_module_disabled") as error:
            await task
        assert error.value.usage.actual_cost_usd == 0
        assert error.value.usage.billing_status == "confirmed"
    finally:
        release.set()
        if not task.done():
            await task
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert port.calls == 0 and balance.hourly_call_count == 1
    assert balance.reserved_usd == balance.spent_usd == 0


@pytest.mark.parametrize("reenable", [False, True])
@pytest.mark.asyncio
async def test_inflight_jev_switch_invalidates_result_but_preserves_known_cost(tmp_path, reenable):
    from agent_platform.ports.model import ModelCallFailed

    entered, release = asyncio.Event(), asyncio.Event()

    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            entered.set()
            await release.wait()
            return valid_response(request, quote)

    port = Port()
    worker, store, request = await setup(tmp_path, port=port)
    task = asyncio.create_task(worker.decide(request))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        worker.set_enabled(False)
        if reenable:
            worker.set_enabled(True)
        release.set()
        with pytest.raises(ModelCallFailed, match="model_module_disabled") as error:
            await task
        assert error.value.usage.actual_cost_usd == Decimal("0.0000084")
    finally:
        release.set()
        if not task.done():
            await task
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert port.calls == 1 and balance.hourly_call_count == 1
    assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0


@pytest.mark.asyncio
async def test_reapplying_jev_enabled_value_does_not_invalidate_inflight_result(tmp_path):
    class Port:
        async def decide(self, request, quote):
            worker.set_enabled(True)
            return valid_response(request, quote)

    worker, _, request = await setup(tmp_path, port=Port())
    assert (await worker.decide(request)).answers[0].choice == "PASS"


@pytest.mark.parametrize("value", [0, 1, "true", None])
@pytest.mark.asyncio
async def test_jev_enablement_is_strict_and_invalid_input_does_not_change_state(tmp_path, value):
    worker, _, _ = await setup(tmp_path)
    with pytest.raises(ValueError):
        worker.set_enabled(value)
    assert worker.enabled is True
