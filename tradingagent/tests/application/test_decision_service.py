"""Real SQLite budget/claims surround synthetic model and rule providers."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.decisions import SqliteDecisionStore
from agent_platform.application.prompting import build_prompt
from agent_platform.application.routing import ModelRouter
from agent_platform.domain.costs import BudgetRequest, BudgetReservation, ModelUsage
from agent_platform.domain.decisions import AdvisoryAssessment, DecisionSnapshot
from agent_platform.domain.events import SessionJournalEvent
from agent_platform.domain.model_calls import ModelRequest, ModelResponse
from agent_platform.domain.risk import RiskContext
from agent_platform.domain.routing import RoutingPolicy
from agent_platform.domain.sessions import TradingStyle
from tests.adapters.test_sqlite_decisions import context as _sqlite_context
from tests.adapters.test_sqlite_decisions import refresh, snapshot
from tests.application.test_routing import tier
from tests.domain.test_decisions import NOW

context = _sqlite_context


def application():
    return importlib.import_module("agent_platform.application.decisions")


class Rules:
    def __init__(self):
        self.calls = 0

    def evaluate(self, value):
        self.calls += 1
        return AdvisoryAssessment(action="hold", explanation="明确配置的测试规则", source="rule")


class Current:
    def __init__(self, context, clock):
        _, self.facts, self.sessions, _, _ = context
        self.clock = clock

    async def capture(self, request):
        account = await self.facts.account_snapshot("local-spot")
        session = await self.sessions.get("session-1")
        value = snapshot(
            account, request.request_id + ":publication", self.clock.utcnow()
        ).model_dump()
        value.update(
            session_revision=session.revision,
            style_revision=session.style_revision,
            style=session.style,
            limits=request.snapshot.limits,
        )
        return DecisionSnapshot(**value)


class RecordedModel:
    """Synthetic owned responses only; verifies reservation exists before invocation."""

    def __init__(self, path, clock, *, change=None, operation=None, wait=False):
        self.path, self.clock = path, clock
        self.change, self.operation, self.wait = change or {}, operation, wait
        self.calls = 0
        self.started = asyncio.Event()

    async def generate(self, request):
        self.calls += 1
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT body FROM budget_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
        assert row is not None, "budget must be committed before supplier invocation"
        reservation = BudgetReservation.model_validate_json(row[0])
        assert reservation.request.route_id == request.route.route_id
        self.started.set()
        if self.operation:
            await self.operation()
        if self.wait:
            await asyncio.Event().wait()
        assessment = dict(
            action="hold", explanation="离线录制的测试响应", source="fake", valid_for_seconds=30
        )
        usage = dict(
            request_id=request.request_id,
            route_id=request.route.route_id,
            model_version=request.route.model_version,
            input_tokens=10,
            output_tokens=10,
            estimated_cost_usd=reservation.request.estimated_cost_usd,
            actual_cost_usd="0",
            billing_status="confirmed",
            recorded_at=self.clock.utcnow(),
        )
        assessment.update(self.change.get("assessment", {}))
        usage.update(self.change.get("usage", {}))
        return ModelResponse(request_id=request.request_id, assessment=assessment, usage=usage)


def service(context, *, model=None, rules=None, daily="1", hourly=60, clock=None, configured=True):
    path, facts, _, _, _ = context
    clock = clock or FakeClock(NOW)
    router = ModelRouter(
        RoutingPolicy(
            routes=(tier(),) if configured else (),
            daily_limit_usd=daily,
            hourly_call_limit=hourly,
        ),
        clock,
    )
    store = SqliteDecisionStore(path, clock=clock)
    return (
        application().DecisionService(
            clock=clock,
            router=router,
            decisions=store,
            budgets=facts,
            model=model,
            rules=rules,
            current=Current(context, clock),
        ),
        store,
        clock,
    )


def prepared(value, account, identity="request-1"):
    return value.prepare(snapshot(account), request_id=identity)


def budget(path):
    with sqlite3.connect(path) as connection:
        return [
            BudgetReservation.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT body FROM budget_requests ORDER BY request_id",
            )
        ]


@pytest.mark.asyncio
async def test_default_configuration_is_unavailable_without_model_or_invented_hold(context):
    _, _, _, _, account = context
    value, _, _ = service(context, configured=False, daily="0")
    result = await value.decide(prepared(value, account))
    assert result.status == "unavailable" and "rule_unconfigured" in result.reasons
    assert result.recommendation is None and not budget(context[0])


@pytest.mark.asyncio
async def test_zero_budget_never_calls_model_but_explicit_rules_can_publish(context):
    clock = FakeClock(NOW)
    model, rules = RecordedModel(context[0], clock), Rules()
    value, _, _ = service(context, model=model, rules=rules, daily="0", clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "published" and result.recommendation.assessment.source == "rule"
    assert model.calls == 0 and rules.calls == 1 and not budget(context[0])


@pytest.mark.asyncio
async def test_model_reservation_and_settlement_precede_publication_and_retry_never_calls_again(
    context,
):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock)
    value, store, _ = service(context, model=model, clock=clock)
    request = prepared(value, context[4])
    first = await value.decide(request)
    assert first.status == "published" and first.recommendation.assessment.source == "fake"
    assert first.usage.billing_status == "confirmed" and budget(context[0])[0].status == "settled"
    reopened, _, _ = service(context, model=model, clock=clock)
    assert await reopened.decide(request) == first
    assert model.calls == 1 and (await store.decision(request.request_id)).result == first


@pytest.mark.asyncio
async def test_persisted_hourly_allowance_is_shared_with_review_and_fallback_is_only_once(context):
    clock = FakeClock(NOW)
    model, rules = RecordedModel(context[0], clock), Rules()
    await context[1].reserve(
        BudgetRequest(
            request_id="prior-review",
            route_id="fixture-review",
            purpose="review",
            price_version="fixture",
            estimated_cost_usd="0",
            daily_limit_usd="1",
            hourly_call_limit=1,
            requested_at=NOW,
        )
    )
    value, _, _ = service(context, model=model, rules=rules, hourly=1, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "published" and result.recommendation.assessment.source == "rule"
    assert model.calls == 0 and rules.calls == 1 and len(budget(context[0])) == 1


@pytest.mark.asyncio
async def test_timeout_keeps_unknown_reservation_and_never_reissues_model(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock, wait=True)
    value, _, _ = service(context, model=model, clock=clock)
    request = prepared(value, context[4]).model_dump()
    request["deadline"] = NOW + timedelta(milliseconds=20)
    request = type(prepared(value, context[4]))(**request)
    result = await value.decide(request)
    assert result.status == "unavailable" and "provider_timeout" in result.reasons
    assert result.usage.billing_status == "unknown" and budget(context[0])[0].status == "unknown"
    assert not result.usage.token_counts_known
    assert budget(context[0])[0].held_cost_usd > 0 and model.calls == 1


@pytest.mark.asyncio
async def test_cancel_records_unknown_exposure_and_does_not_consume_a_second_attempt(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock, wait=True)
    value, store, _ = service(context, model=model, clock=clock)
    request = prepared(value, context[4])
    task = asyncio.create_task(value.decide(request))
    await model.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget(context[0])[0].status == "unknown"
    assert (await store.decision(request.request_id)).result.reasons == ("provider_cancelled",)
    assert model.calls == 1


@pytest.mark.asyncio
async def test_parallel_or_crash_pending_request_is_not_retried(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock, wait=True)
    value, store, _ = service(context, model=model, clock=clock)
    request = prepared(value, context[4])
    await store.claim(request)
    result = await value.decide(request)
    assert result.status == "unavailable" and result.reasons == ("request_in_flight",)
    assert model.calls == 0 and not budget(context[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"assessment": {"action": "buy", "evidence_ids": ["invented-fact"]}},
        {"usage": {"model_version": "forged-model"}},
        {"usage": {"actual_cost_usd": "1e1000000"}},
    ],
)
async def test_invalid_model_fact_never_becomes_current_advice_and_only_rules_may_fall_back(
    context, change
):
    clock = FakeClock(NOW)
    model, rules = RecordedModel(context[0], clock, change=change), Rules()
    value, _, _ = service(context, model=model, rules=rules, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "published" and result.recommendation.assessment.source == "rule"
    assert rules.calls == 1 and model.calls == 1
    if "usage" in change:
        assert (
            result.usage.billing_status == "unknown" and budget(context[0])[0].status == "unknown"
        )


@pytest.mark.asyncio
async def test_valid_over_estimate_fee_is_recorded_and_freezes_next_paid_call(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock, change={"usage": {"actual_cost_usd": "0.1"}})
    value, _, _ = service(context, model=model, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.usage.actual_cost_usd == Decimal("0.1")
    assert budget(context[0])[0].actual_cost_usd == Decimal("0.1")
    another = await value.decide(prepared(value, context[4], "request-2"))
    assert another.status == "unavailable" and model.calls == 1


@pytest.mark.asyncio
async def test_style_change_during_model_wait_preserves_original_assessment_as_superseded(context):
    clock = FakeClock(NOW)

    async def change_style():
        changed = context[3].change_style(TradingStyle(strength=0), NOW)
        await context[2].save(
            changed,
            2,
            SessionJournalEvent(
                event_id="style-in-flight",
                kind="style_changed",
                session=changed,
                occurred_at=NOW,
            ),
        )

    model = RecordedModel(context[0], clock, operation=change_style)
    value, _, _ = service(context, model=model, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "superseded" and result.recommendation.style_revision == 1
    assert result.recommendation.original_author == "agent"


@pytest.mark.asyncio
async def test_account_change_during_model_wait_is_superseded_without_rewriting_input(context):
    clock = FakeClock(NOW)

    async def change_account():
        await refresh(context[1], context[4], free="999", at=NOW)

    model = RecordedModel(context[0], clock, operation=change_account)
    value, _, _ = service(context, model=model, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "superseded" and result.recommendation.account_revision == 1
    assert result.reasons == ("account_changed",)


@pytest.mark.asyncio
async def test_current_risk_blocks_quantity_without_user_limits_after_billing_is_recorded(context):
    clock = FakeClock(NOW)
    model = RecordedModel(
        context[0],
        clock,
        change={
            "assessment": {
                "action": "buy",
                "evidence_ids": ["features-1"],
                "quantity": "0.1",
            }
        },
    )
    rules = Rules()
    value, _, _ = service(context, model=model, rules=rules, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "unavailable" and "quantity_limit_unconfigured" in result.reasons
    assert result.usage.billing_status == "confirmed" and rules.calls == 0


@pytest.mark.asyncio
async def test_old_input_ttl_is_not_refreshed_by_a_late_model_response(context):
    clock = FakeClock(NOW)

    async def delay():
        clock.advance_to(NOW + timedelta(seconds=2))

    model = RecordedModel(
        context[0], clock, operation=delay, change={"assessment": {"valid_for_seconds": 1}}
    )
    value, _, _ = service(context, model=model, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "unavailable" and "advice_expired" in result.reasons
    assert result.recommendation is None and result.usage.billing_status == "confirmed"


@pytest.mark.asyncio
async def test_unready_input_does_not_reserve_budget_or_call_model(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock)
    value, _, _ = service(context, model=model, clock=clock)
    bad = snapshot(context[4]).model_dump()
    bad["features"]["warmup_ready"] = False
    result = await value.decide(value.prepare(DecisionSnapshot(**bad), request_id="request-1"))
    assert result.status == "unavailable" and "features_not_ready" in result.reasons
    assert model.calls == 0 and not budget(context[0])


@pytest.mark.asyncio
async def test_late_assessment_cannot_erase_valid_charge_or_publish_after_deadline(context):
    clock = FakeClock(NOW)

    async def delay():
        clock.advance_to(NOW + timedelta(seconds=16))

    model = RecordedModel(context[0], clock, operation=delay)
    value, _, _ = service(context, model=model, rules=Rules(), clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "unavailable" and result.recommendation is None
    assert result.usage.billing_status == "confirmed" and budget(context[0])[0].status == "settled"
    assert model.calls == 1


def test_unknown_token_counts_cannot_be_presented_as_known_nonzero_measurements():
    with pytest.raises(ValueError):
        ModelUsage(
            request_id="request-1",
            route_id="fixture",
            model_version="fixture",
            input_tokens=10,
            output_tokens=0,
            token_counts_known=False,
            estimated_cost_usd="0.01",
            billing_status="unknown",
            recorded_at=NOW,
        )


@pytest.mark.asyncio
async def test_post_response_risk_uses_current_ask_instead_of_original_book(context):
    clock = FakeClock(NOW)
    model = RecordedModel(
        context[0],
        clock,
        change={
            "assessment": {
                "action": "buy",
                "evidence_ids": ["features-1"],
                "quantity": "0.015",
            }
        },
    )
    value, _, _ = service(context, model=model, clock=clock)
    original = snapshot(context[4]).model_dump()
    original["limits"] = dict(max_buy_quantity="0.02", max_position_quantity="1")
    request = value.prepare(DecisionSnapshot(**original), request_id="request-1")
    value.risk_context = RiskContext(
        instrument=dict(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            filter_version="fixture-v1",
            price_tick="1",
            quantity_step="0.001",
            min_quantity="0.001",
            min_notional="0",
        ),
        cost_buffer_rate="0",
        cost_policy_version="fixture-v1",
    )
    current_capture = value.current.capture

    async def more_expensive(req):
        publication = (await current_capture(req)).model_dump()
        publication["market"]["book"]["ask"] = "90000"
        return DecisionSnapshot(**publication)

    value.current.capture = more_expensive
    result = await value.decide(request)
    assert result.status == "unavailable" and "insufficient_free_balance" in result.reasons
    assert result.usage.billing_status == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["future", "scope", "same_id"])
async def test_invalid_current_provider_evidence_closes_advice_but_keeps_settled_usage(
    context, fault
):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock)
    value, _, _ = service(context, model=model, clock=clock)
    capture = value.current.capture

    async def invalid(req):
        evidence = (await capture(req)).model_dump()
        if fault == "future":
            evidence["captured_at"] = NOW + timedelta(seconds=1)
        elif fault == "scope":
            evidence["account"]["account_ref"] = "another-account"
        else:
            evidence["snapshot_id"] = req.snapshot.snapshot_id
        return DecisionSnapshot(**evidence)

    value.current.capture = invalid
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "unavailable" and result.reasons == ("current_evidence_invalid",)
    assert result.usage.billing_status == "confirmed" and result.recommendation is None


@pytest.mark.asyncio
async def test_expired_request_before_reserve_has_no_unknown_bill_or_call(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock)
    value, _, _ = service(context, model=model, clock=clock)
    request = prepared(value, context[4]).model_dump()
    request["deadline"] = NOW + timedelta(milliseconds=500)
    request = type(prepared(value, context[4]))(**request)
    clock.advance_to(NOW + timedelta(seconds=1))
    result = await value.decide(request)
    assert result.status == "unavailable" and result.reasons == ("request_expired",)
    assert not budget(context[0]) and model.calls == 0 and result.usage is None


@pytest.mark.asyncio
async def test_expiry_during_reserve_is_known_zero_cost_because_model_was_never_dispatched(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock)
    value, _, _ = service(context, model=model, clock=clock)
    request = prepared(value, context[4]).model_dump()
    request["deadline"] = NOW + timedelta(milliseconds=500)
    request = type(prepared(value, context[4]))(**request)
    original = value.budgets

    class DelayedReservation:
        async def reserve(self, req, **flags):
            reservation = await original.reserve(req, **flags)
            clock.advance_to(NOW + timedelta(seconds=1))
            return reservation

        async def settle(self, identifier, usage):
            return await original.settle(identifier, usage)

    value.budgets = DelayedReservation()
    result = await value.decide(request)
    assert result.status == "unavailable" and model.calls == 0
    assert result.usage.billing_status == "confirmed" and result.usage.actual_cost_usd == 0
    assert budget(context[0])[0].held_cost_usd == 0


@pytest.mark.asyncio
async def test_provider_usage_before_this_reservation_is_unknown_and_fallback_remains_once(context):
    clock = FakeClock(NOW + timedelta(seconds=1))
    model = RecordedModel(context[0], clock, change={"usage": {"recorded_at": NOW}})
    rules = Rules()
    value, _, _ = service(context, model=model, rules=rules, clock=clock)
    result = await value.decide(prepared(value, context[4]))
    assert result.status == "published" and result.recommendation.assessment.source == "rule"
    assert result.usage.billing_status == "unknown" and rules.calls == 1 and model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["reserved", "unknown", "settled"])
async def test_prior_budget_evidence_never_grants_another_dispatch_or_replaces_old_exposure(
    context, status
):
    clock = FakeClock(NOW)
    model, rules = RecordedModel(context[0], clock), Rules()
    value, _, _ = service(context, model=model, rules=rules, clock=clock)
    request = prepared(value, context[4])
    model_request = ModelRequest(
        request_id=request.request_id,
        snapshot=request.snapshot,
        route=request.route,
        purpose="advisory",
        deadline=request.deadline,
        max_output_tokens=100,
        prompt_version=request.prompt_version,
    )
    quote = value.router.quote(request.route, build_prompt(model_request))
    reservation = await context[1].reserve(
        BudgetRequest(
            request_id=request.request_id,
            route_id=request.route.route_id,
            purpose="advisory",
            price_version=quote.price_version,
            estimated_cost_usd=quote.estimated_cost_usd,
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    if status != "reserved":
        await context[1].settle(
            reservation.reservation_id,
            ModelUsage(
                request_id=request.request_id,
                route_id=request.route.route_id,
                model_version=request.route.model_version,
                input_tokens=0,
                output_tokens=0,
                token_counts_known=status == "settled",
                estimated_cost_usd=quote.estimated_cost_usd,
                billing_status="confirmed" if status == "settled" else "unknown",
                actual_cost_usd="0.01" if status == "settled" else None,
                recorded_at=NOW,
            ),
        )
    before = budget(context[0])[0]
    result = await value.decide(request)
    assert model.calls == 0 and rules.calls == 1
    assert result.status == "published" and result.recommendation.assessment.source == "rule"
    assert budget(context[0])[0] == before


@pytest.mark.asyncio
async def test_price_expiry_while_reserving_never_dispatches_under_a_stale_quote(context):
    clock = FakeClock(NOW)
    model = RecordedModel(context[0], clock)
    value, _, _ = service(context, model=model, clock=clock)
    candidate = tier()
    candidate["price"]["valid_until"] = NOW + timedelta(milliseconds=500)
    value.router = ModelRouter(RoutingPolicy(routes=(candidate,), daily_limit_usd="1"), clock)
    request = prepared(value, context[4])
    original = value.budgets

    class DelayedReservation:
        async def reserve(self, req, **flags):
            reservation = await original.reserve(req, **flags)
            clock.advance_to(NOW + timedelta(seconds=1))
            return reservation

        async def settle(self, identifier, usage):
            return await original.settle(identifier, usage)

    value.budgets = DelayedReservation()
    result = await value.decide(request)
    assert model.calls == 0 and result.status == "unavailable"
    assert result.usage.billing_status == "confirmed" and result.usage.actual_cost_usd == 0
    assert budget(context[0])[0].held_cost_usd == 0
