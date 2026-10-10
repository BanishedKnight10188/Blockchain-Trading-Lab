"""Execution observations preserve original advice without guessing complete orders."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.account import AccountSnapshot
from agent_platform.domain.decision_requests import DecisionReadRecord
from agent_platform.domain.decisions import AdvisoryAssessment
from agent_platform.domain.reviews import TradeAttribution
from tests.adapters.test_sqlite_decisions import completion, request
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_attribution import change, setup
from tests.application.test_trade_groups import fill
from tests.domain.test_decisions import NOW

context = _context


def checker():
    return importlib.import_module("agent_platform.domain.review_checks").trade_review_check


def evidence(action="hold", quantity=None):
    account = AccountSnapshot(account_ref="local-spot", as_of=NOW)
    completed = completion(account)
    assessment = AdvisoryAssessment(
        action=action,
        quantity=quantity,
        explanation="原始建议",
        source="fake",
        evidence_ids=("features-1",),
    )
    advice = completed.result.recommendation.model_copy(update={"assessment": assessment})
    completed = completed.model_copy(
        update={"result": completed.result.model_copy(update={"recommendation": advice})}
    )
    return DecisionReadRecord(request=request(account), completion=completed)


def attribution(trade, linked=True):
    return TradeAttribution(
        account_ref=trade.account_ref,
        symbol=trade.symbol,
        trade_id=trade.trade_id,
        original_author="agent" if linked else "unclassified",
        final_decision_maker="human" if linked else "unclassified",
        recommendation_id="advice-1" if linked else None,
        user_confirmed=linked,
    )


@pytest.mark.parametrize("action,deviation", [("hold", True), ("buy", False), ("sell", True)])
def test_checks_compare_actual_side_and_keep_original_style_and_fee(action, deviation):
    trade = fill("1", "buy", "0.01", "600", fee="0.6", asset="USDT")
    result = checker()(trade, attribution(trade), evidence(action))
    assert result.action_deviation is deviation and result.expired_at_execution is False
    assert (result.original_style.strength, result.original_style_revision) == (67, 1)
    assert result.original_model_usage is None and result.usage_status == "not_recorded"
    assert (result.fee, result.fee_asset) == (Decimal("0.6"), "USDT")
    assert result.publication_risk.outcome == "allow"
    assert result.execution_discipline_status == "unavailable"


@pytest.mark.parametrize("second,expired", [(59, False), (60, True), (61, True)])
def test_execution_expiry_uses_original_deadline_not_current_projection(second, expired):
    trade = fill("1", "buy", "0.01", "600", second=second)
    source = evidence("buy")
    current = source.completion.result.recommendation.transition("rejected", NOW)
    source = source.model_copy(update={"current_recommendation": current})
    assert checker()(trade, attribution(trade), source).expired_at_execution is expired


@pytest.mark.parametrize(
    "quantity,status", [("0.01", "not_assessed"), ("0.03", "exceeds_original")]
)
def test_partial_fill_cannot_prove_whole_order_quantity_compliance(quantity, status):
    trade = fill("1", "buy", quantity, "600")
    result = checker()(trade, attribution(trade), evidence("buy", "0.02"))
    assert result.quantity_check == status


def test_unclassified_fill_preserves_unknown_checks():
    trade = fill("1", "buy", "0.01", "600")
    result = checker()(trade, attribution(trade, linked=False), None)
    assert result.action_deviation is result.expired_at_execution is None
    assert result.original_style is result.publication_risk is None
    assert result.usage_status == "not_linked" and result.quantity_check == "not_linked"


@pytest.mark.parametrize("status,cost", [("unknown", None), ("confirmed", "0.05")])
def test_original_model_billing_is_kept_without_claiming_review_called_a_model(status, cost):
    from agent_platform.domain.costs import ModelUsage

    trade = fill("1", "buy", "0.01", "600")
    source = evidence("buy")
    usage = ModelUsage(
        request_id="request-1",
        route_id="test-model",
        model_version="fake-v1",
        input_tokens=0,
        output_tokens=0,
        token_counts_known=False,
        estimated_cost_usd="0.05",
        actual_cost_usd=cost,
        billing_status=status,
        recorded_at=NOW,
    )
    source = source.model_copy(
        update={
            "completion": source.completion.model_copy(
                update={"result": source.completion.result.model_copy(update={"usage": usage})}
            )
        }
    )
    result = checker()(trade, attribution(trade), source)
    assert result.usage_status == "recorded" and result.original_model_usage == usage
    assert result.original_model_usage.token_counts_known is False


@pytest.mark.parametrize("case", ["foreign", "future", "other_trade", "missing"])
def test_checks_reject_unrelated_or_unavailable_original_evidence(case):
    trade = fill("1", "buy", "0.01", "600", second=-1 if case == "future" else 0)
    linked = attribution(trade)
    if case == "foreign":
        trade = trade.model_copy(update={"account_ref": "foreign"})
    if case == "other_trade":
        linked = linked.model_copy(update={"trade_id": "another"})
    with pytest.raises(ValueError):
        checker()(trade, linked, None if case == "missing" else evidence("buy"))


def test_original_request_cannot_hide_future_snapshot_behind_earlier_completion():
    from tests.adapters.test_sqlite_decisions import snapshot

    trade = fill("1", "buy", "0.01", "600")
    source = evidence("buy")
    later = NOW + timedelta(seconds=1)
    source = source.model_copy(
        update={
            "request": source.request.model_copy(
                update={
                    "snapshot": snapshot(source.request.snapshot.account, at=later),
                    "requested_at": later,
                }
            )
        }
    )
    with pytest.raises(ValueError):
        checker()(trade, attribution(trade), source)


@pytest.mark.asyncio
async def test_linked_checks_freeze_and_reopen_after_attribution_correction(context):
    attribution_service, attribution_store, _ = await setup(context, trade_at=NOW)
    await attribution_service.record(change())
    app = importlib.import_module("agent_platform.application.reviews")
    adapter = importlib.import_module("agent_platform.adapters.sqlite.reviews")
    clock = attribution_service.clock
    store = adapter.SqliteReviewStore(context[0], clock=clock)
    value = app.ReviewService(store=store, clock=clock, account_ref="local-spot")
    await value.create_group("checks-group", NOW)
    await value.generate("checks-group", NOW, "initial")
    first = (await store.review_versions("checks-group", account_ref="local-spot"))[0]
    assert first.trade_checks[0].action_deviation is True
    clock.advance_to(NOW + timedelta(seconds=1))
    corrected = change("correct-human", 2, agent=False).model_copy(
        update={"recorded_at": clock.utcnow()}
    )
    await attribution_service.record(corrected)
    await value.generate("checks-group", clock.utcnow(), "followup")
    reopened = type(store)(context[0], clock=clock)
    versions = await reopened.review_versions("checks-group", account_ref="local-spot")
    assert versions[0] == first
    assert versions[1].trade_checks[0].usage_status == "not_linked"
    assert (
        await attribution_store.recommendation_decision("advice-1")
    ).request.snapshot.style.strength == 67
