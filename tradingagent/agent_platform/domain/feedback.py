"""Immutable human intent and the evidence needed to commit a current response."""

from datetime import timedelta
from hashlib import sha256
from typing import Self

from pydantic import model_validator

from .decision_requests import DecisionCompletion, DecisionRequest
from .decisions import DecisionFeedback, DecisionSnapshot, Recommendation
from .models import DomainModel, Identifier, Revision
from .risk import RiskAssessment


class FeedbackRecord(DomainModel):
    feedback: DecisionFeedback
    account_ref: Identifier
    recommendation_state: Recommendation | None = None
    recommendation_revision: Revision | None = None

    @model_validator(mode="after")
    def bounded_intent(self) -> Self:
        value = self.feedback
        frozen = self.recommendation_state
        if (frozen is None) != (self.recommendation_revision is None):
            raise ValueError("feedback recommendation commit metadata is incomplete")
        if frozen is not None:
            expected = (
                "accepted"
                if value.kind == "accepted"
                else ("rejected" if value.recorded_at < frozen.expires_at else "expired")
            )
            if (
                value.recommendation_id != frozen.recommendation_id
                or frozen.status != expected
                or frozen.updated_at != value.recorded_at
                or self.recommendation_revision < 3
            ):
                raise ValueError("feedback commit must match its terminal recommendation state")
        if (
            self.account_ref.startswith("paper:")
            or any(
                len(item) > 128
                for item in (value.feedback_id, self.account_ref, value.recommendation_id or "")
            )
            or len(value.explanation) > 4096
        ):
            raise ValueError("feedback scope or text is outside the bounded contract")
        idea = value.modified_assessment
        if idea is not None:
            if (
                len(idea.explanation) > 4096
                or len(idea.evidence_ids) > 64
                or any(len(item) > 128 for item in idea.evidence_ids)
            ):
                raise ValueError("human idea exceeds the bounded contract")
            quantity = idea.quantity
            if quantity is not None and (
                len(quantity.as_tuple().digits) > 128
                or not -128 <= quantity.as_tuple().exponent <= 128
            ):
                raise ValueError("human quantity exceeds the bounded contract")
        return self

    @property
    def aggregate_id(self) -> str:
        return "feedback:" + sha256(self.feedback.feedback_id.encode()).hexdigest()

    @property
    def updated_at(self):
        return self.feedback.recorded_at


class FeedbackEvidence(DomainModel):
    request: DecisionRequest
    completion: DecisionCompletion
    current: DecisionSnapshot | None = None
    expected_revision: Revision
    risk: RiskAssessment | None = None

    def valid_until(self):
        snapshot, advice = self.current, self.completion.result.recommendation
        if snapshot is None or advice is None or snapshot.market.latest_quote_at is None:
            return self.completion.completed_at
        times = [
            advice.expires_at,
            snapshot.trigger.expires_at,
            snapshot.market.latest_quote_at + timedelta(seconds=5),
            snapshot.features.as_of + timedelta(seconds=60),
            snapshot.account.as_of + timedelta(seconds=60),
        ]
        if advice.assessment.quantity is not None:
            if snapshot.market.book_as_of is None:
                return snapshot.captured_at
            times.append(snapshot.market.book_as_of + timedelta(seconds=5))
        if advice.assessment.action == "buy" and snapshot.limits.max_daily_loss_usd is not None:
            context = self.completion.risk_context
            if not context.daily_loss_complete or context.daily_loss_as_of is None:
                return snapshot.captured_at
            times.append(context.daily_loss_as_of + timedelta(seconds=60))
            from zoneinfo import ZoneInfo

            local = snapshot.captured_at.astimezone(ZoneInfo("Asia/Shanghai"))
            times.append(
                (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            )
        return min(times)
