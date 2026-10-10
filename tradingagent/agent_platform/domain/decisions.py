"""Frozen evidence and advice; user feedback never rewrites the original author."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from .account import AccountSnapshot, PositionView
from .common import utc_datetime
from .costs import ModelUsage
from .market import FeatureSnapshot, MarketSnapshot
from .models import DomainModel, Identifier, PositiveAmount, Revision, UtcDateTime
from .risk import DisciplineLimits, RiskAssessment
from .sessions import TradingStyle


class TriggerKind(StrEnum):
    PERIODIC = "periodic"
    MARKET_CHANGE = "market_change"
    ACCOUNT_CHANGE = "account_change"
    MANUAL = "manual"
    HARD_RISK = "hard_risk"


class DecisionEvent(DomainModel):
    event_id: Identifier
    session_id: Identifier
    kind: TriggerKind
    occurred_at: UtcDateTime
    expires_at: UtcDateTime
    priority: int = Field(default=0, strict=True, ge=0, le=100)
    evidence_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.expires_at <= self.occurred_at:
            raise ValueError("decision event needs a positive lifetime")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence identifiers must be unique")
        return self


class DecisionTrigger(DomainModel):
    trigger_id: Identifier
    session_id: Identifier
    kind: TriggerKind
    requested_at: UtcDateTime
    expires_at: UtcDateTime
    event_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("decision trigger needs a positive lifetime")
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("trigger event identifiers must be unique")
        return self


class DecisionSnapshot(DomainModel):
    snapshot_id: Identifier
    session_id: Identifier
    session_revision: Revision
    style_revision: Revision
    style: TradingStyle
    captured_at: UtcDateTime
    market: MarketSnapshot
    features: FeatureSnapshot
    account: AccountSnapshot
    position: PositionView
    trigger: DecisionTrigger
    limits: DisciplineLimits = Field(default_factory=DisciplineLimits)
    evidence_ids: tuple[Identifier, ...]

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        if self.style_revision > self.session_revision:
            raise ValueError("style revision cannot exceed session revision")
        if len({self.market.symbol, self.features.symbol, self.position.symbol}) != 1:
            raise ValueError("decision evidence describes different symbols")
        if any(
            item.as_of > self.captured_at for item in (self.market, self.features, self.account)
        ):
            raise ValueError("decision evidence cannot contain future observations")
        if self.trigger.session_id != self.session_id:
            raise ValueError("decision trigger belongs to another session")
        if not self.trigger.requested_at <= self.captured_at < self.trigger.expires_at:
            raise ValueError("decision trigger is not current at capture time")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("decision evidence identifiers must be unique")
        return self


class AdviceAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    UNAVAILABLE = "unavailable"


class AdvisoryAssessment(DomainModel):
    action: AdviceAction
    explanation: Identifier
    source: Literal["rule", "fake", "model", "human"]
    evidence_ids: tuple[Identifier, ...] = ()
    quantity: PositiveAmount | None = None
    unavailable_reasons: tuple[Identifier, ...] = ()
    valid_for_seconds: int = Field(default=60, strict=True, ge=1, le=300)

    @model_validator(mode="after")
    def honest_advice(self) -> Self:
        if (
            self.action in (AdviceAction.HOLD, AdviceAction.UNAVAILABLE)
            and self.quantity is not None
        ):
            raise ValueError("hold or unavailable advice cannot specify trade quantity")
        if (self.action == AdviceAction.UNAVAILABLE) != bool(self.unavailable_reasons):
            raise ValueError("unavailable advice needs explicit reasons")
        if self.action in (AdviceAction.BUY, AdviceAction.SELL) and not self.evidence_ids:
            raise ValueError("trade advice requires evidence references")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("advice evidence identifiers must be unique")
        return self


class RecommendationStatus(StrEnum):
    CREATED = "created"
    PUBLISHED = "published"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


_TRANSITIONS = {
    RecommendationStatus.CREATED: frozenset(
        (
            RecommendationStatus.PUBLISHED,
            RecommendationStatus.EXPIRED,
            RecommendationStatus.SUPERSEDED,
        )
    ),
    RecommendationStatus.PUBLISHED: frozenset(
        (
            RecommendationStatus.ACCEPTED,
            RecommendationStatus.REJECTED,
            RecommendationStatus.EXPIRED,
            RecommendationStatus.SUPERSEDED,
        )
    ),
}


class Recommendation(DomainModel):
    recommendation_id: Identifier
    snapshot_id: Identifier
    session_id: Identifier
    style_revision: Revision
    account_revision: int = Field(strict=True, ge=0)
    original_author: Literal["agent"] = "agent"
    assessment: AdvisoryAssessment
    status: RecommendationStatus = RecommendationStatus.CREATED
    created_at: UtcDateTime
    updated_at: UtcDateTime
    expires_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_timing(self) -> Self:
        if self.updated_at < self.created_at or self.expires_at <= self.created_at:
            raise ValueError("recommendation chronology is inconsistent")
        if self.status == RecommendationStatus.EXPIRED and self.updated_at < self.expires_at:
            raise ValueError("recommendation cannot expire before its validity ends")
        if (
            self.status
            in (
                RecommendationStatus.PUBLISHED,
                RecommendationStatus.ACCEPTED,
                RecommendationStatus.REJECTED,
            )
            and self.updated_at >= self.expires_at
        ):
            raise ValueError("expired advice cannot be published or accepted as current")
        if self.assessment.source == "human":
            raise ValueError("human ideas belong in feedback, not agent recommendations")
        return self

    def transition(self, status: RecommendationStatus | str, at: datetime) -> Self:
        target = RecommendationStatus(status)
        timestamp = utc_datetime(at)
        if timestamp < self.updated_at:
            raise ValueError("recommendation updates cannot move back in time")
        if target == self.status:
            return self
        if target not in _TRANSITIONS.get(self.status, ()):
            raise ValueError("recommendation state transition is not allowed")
        return type(self).model_validate(
            {**self.model_dump(), "status": target, "updated_at": timestamp}
        )


class FeedbackKind(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MODIFIED = "modified"
    INDEPENDENT = "independent"


class DecisionFeedback(DomainModel):
    feedback_id: Identifier
    kind: FeedbackKind
    recommendation_id: Identifier | None = None
    modified_assessment: AdvisoryAssessment | None = None
    explanation: Identifier
    recorded_at: UtcDateTime
    final_decision_maker: Literal["human"] = "human"

    @model_validator(mode="after")
    def explicit_relation(self) -> Self:
        if (self.kind == FeedbackKind.INDEPENDENT) != (self.recommendation_id is None):
            raise ValueError("feedback relation must agree with its kind")
        requires_idea = self.kind in (FeedbackKind.MODIFIED, FeedbackKind.INDEPENDENT)
        if requires_idea != (self.modified_assessment is not None):
            raise ValueError("modified or independent feedback requires the user's idea")
        if self.modified_assessment is not None and self.modified_assessment.source != "human":
            raise ValueError("feedback idea must be explicitly authored by the user")
        return self


class FeedbackReceipt(DomainModel):
    feedback: DecisionFeedback
    event_id: Identifier


class DecisionResult(DomainModel):
    request_id: Identifier
    snapshot_id: Identifier
    publication_snapshot_id: Identifier | None = None
    status: Literal["published", "unavailable", "superseded"]
    recommendation: Recommendation | None = None
    risk: RiskAssessment
    usage: ModelUsage | None = None
    reasons: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        if self.risk.snapshot_id != (self.publication_snapshot_id or self.snapshot_id):
            raise ValueError("risk result belongs to another snapshot")
        if self.usage is not None and self.usage.request_id != self.request_id:
            raise ValueError("usage belongs to another request")
        advice = self.recommendation
        if advice is not None and advice.snapshot_id != self.snapshot_id:
            raise ValueError("recommendation belongs to another snapshot")
        if self.status == "published":
            if (
                advice is None
                or advice.status != RecommendationStatus.PUBLISHED
                or advice.assessment.action == AdviceAction.UNAVAILABLE
                or not self.risk.permits_advice
                or self.reasons
            ):
                raise ValueError("publication requires current advice and allowed risk")
        else:
            if not self.reasons:
                raise ValueError("unavailable or superseded result requires reasons")
            if self.status == "unavailable" and advice is not None:
                raise ValueError("unavailable result cannot publish advice")
            if self.status == "superseded" and (
                advice is None or advice.status != RecommendationStatus.SUPERSEDED
            ):
                raise ValueError("superseded result requires the superseded recommendation")
        return self
