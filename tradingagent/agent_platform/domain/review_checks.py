"""Observable execution comparisons; no claim about unobserved order or account state."""

from typing import Literal

from pydantic import StrictBool

from .account import ObservedTrade
from .costs import ModelUsage
from .decision_requests import DecisionReadRecord
from .decisions import AdviceAction
from .models import (
    DomainModel,
    Identifier,
    NonnegativeAmount,
    PositiveAmount,
    Revision,
    UtcDateTime,
)
from .reviews import TradeAttribution
from .risk import RiskAssessment
from .sessions import TradingStyle


class TradeReviewCheck(DomainModel):
    trade_id: Identifier
    fee: NonnegativeAmount
    fee_asset: Identifier
    recommendation_id: Identifier | None = None
    original_request_id: Identifier | None = None
    original_snapshot_id: Identifier | None = None
    publication_snapshot_id: Identifier | None = None
    original_style: TradingStyle | None = None
    original_style_revision: Revision | None = None
    original_action: AdviceAction | None = None
    original_quantity: PositiveAmount | None = None
    original_expires_at: UtcDateTime | None = None
    publication_risk: RiskAssessment | None = None
    action_deviation: StrictBool | None = None
    expired_at_execution: StrictBool | None = None
    quantity_check: Literal["not_linked", "not_assessed", "exceeds_original"] = "not_linked"
    execution_discipline_status: Literal["unavailable"] = "unavailable"
    original_model_usage: ModelUsage | None = None
    usage_status: Literal["not_linked", "not_recorded", "recorded"] = "not_linked"


def trade_review_check(
    trade: ObservedTrade,
    attribution: TradeAttribution,
    decision: DecisionReadRecord | None,
) -> TradeReviewCheck:
    trade = ObservedTrade.model_validate_json(trade.model_dump_json())
    attribution = TradeAttribution.model_validate_json(attribution.model_dump_json())
    if (
        trade.account_ref != attribution.account_ref
        or trade.symbol != attribution.symbol
        or trade.market_type != attribution.market_type
        or trade.trade_id != attribution.trade_id
    ):
        raise ValueError("execution check requires matching attribution scope")
    base = dict(trade_id=trade.trade_id, fee=trade.fee, fee_asset=trade.fee_asset)
    if attribution.recommendation_id is None:
        if decision is not None:
            raise ValueError("unlinked execution cannot include advice evidence")
        return TradeReviewCheck(**base)
    if decision is None:
        raise ValueError("linked execution requires its original published advice")
    decision = DecisionReadRecord.model_validate_json(decision.model_dump_json())
    completed, snapshot = decision.completion, decision.request.snapshot
    advice = completed.result.recommendation if completed else None
    if (
        completed is None
        or completed.result.status != "published"
        or advice is None
        or advice.recommendation_id != attribution.recommendation_id
        or snapshot.account.account_ref != trade.account_ref
        or snapshot.account.market_type != trade.market_type
        or snapshot.market.symbol != trade.symbol
        or not snapshot.captured_at <= decision.request.requested_at <= completed.completed_at
        or advice.snapshot_id != snapshot.snapshot_id
        or advice.style_revision != snapshot.style_revision
        or completed.completed_at > trade.executed_at
    ):
        raise ValueError("execution check cannot reference future or foreign advice")
    quantity = advice.assessment.quantity
    return TradeReviewCheck(
        **base,
        recommendation_id=advice.recommendation_id,
        original_request_id=decision.request.request_id,
        original_snapshot_id=snapshot.snapshot_id,
        publication_snapshot_id=completed.result.publication_snapshot_id,
        original_style=snapshot.style,
        original_style_revision=snapshot.style_revision,
        original_action=advice.assessment.action,
        original_quantity=quantity,
        original_expires_at=advice.expires_at,
        publication_risk=completed.result.risk,
        action_deviation=str(advice.assessment.action) != str(trade.side),
        expired_at_execution=trade.executed_at >= advice.expires_at,
        quantity_check=(
            "exceeds_original"
            if quantity is not None and trade.quantity > quantity
            else "not_assessed"
        ),
        original_model_usage=completed.result.usage,
        usage_status="recorded" if completed.result.usage is not None else "not_recorded",
    )
