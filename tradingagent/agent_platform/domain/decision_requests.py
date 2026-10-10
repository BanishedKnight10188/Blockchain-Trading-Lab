"""Immutable decision claims and bounded evidence for transactional publication."""

from datetime import timedelta
from typing import Self

from pydantic import StrictBool, model_validator

from .costs import RouteDecision
from .decisions import DecisionResult, DecisionSnapshot, Recommendation
from .model_calls import ModelResponse
from .models import DomainModel, Identifier, UtcDateTime
from .risk import RiskContext


class DecisionRequest(DomainModel):
    request_id: Identifier
    snapshot: DecisionSnapshot
    route: RouteDecision
    requested_at: UtcDateTime
    deadline: UtcDateTime
    prompt_version: Identifier

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if not self.snapshot.captured_at <= self.requested_at < self.deadline:
            raise ValueError("decision request chronology is inconsistent")
        if self.deadline > self.snapshot.trigger.expires_at:
            raise ValueError("decision deadline exceeds its trigger lifetime")
        if len(self.request_id) > 128:
            raise ValueError("decision request identifier exceeds its limit")
        return self


class DecisionClaimReceipt(DomainModel):
    request: DecisionRequest
    claimed: StrictBool = False
    result: DecisionResult | None = None

    @model_validator(mode="after")
    def coherent_receipt(self) -> Self:
        if self.result is not None and (
            self.claimed
            or self.result.request_id != self.request.request_id
            or self.result.snapshot_id != self.request.snapshot.snapshot_id
        ):
            raise ValueError("decision receipt does not match its immutable request")
        return self


class DecisionCompletion(DomainModel):
    result: DecisionResult
    completed_at: UtcDateTime
    publication_snapshot: DecisionSnapshot | None = None
    risk_context: RiskContext = RiskContext()
    response: ModelResponse | None = None
    model_failure: Identifier | None = None

    @property
    def request_id(self) -> str:
        return self.result.request_id

    @model_validator(mode="after")
    def coherent_completion(self) -> Self:
        snapshot = self.publication_snapshot
        if self.result.risk.evaluated_at > self.completed_at:
            raise ValueError("completion cannot include future risk")
        if (snapshot is None) != (self.result.publication_snapshot_id is None):
            raise ValueError("publication risk requires its distinct frozen snapshot")
        if snapshot is not None and (
            snapshot.snapshot_id != self.result.publication_snapshot_id
            or snapshot.captured_at > self.completed_at
            or snapshot.captured_at > self.result.risk.evaluated_at
            or snapshot.snapshot_id == self.result.snapshot_id
        ):
            raise ValueError("publication snapshot does not match risk evidence")
        if self.result.status == "published" and snapshot is None:
            raise ValueError("publication requires current frozen evidence")
        if self.response is not None and (
            self.response.request_id != self.result.request_id
            or self.result.usage != self.response.usage
        ):
            raise ValueError("completion response and usage do not match")
        advice = self.result.recommendation
        if advice is not None and advice.updated_at != self.completed_at:
            raise ValueError("recommendation update time must match completion")
        return self

    def valid_until(self, request: DecisionRequest):
        """A storage-lock delay cannot extend the checks performed by the caller."""
        snapshot = self.publication_snapshot
        advice = self.result.recommendation
        if snapshot is None or advice is None or snapshot.market.latest_quote_at is None:
            return self.completed_at
        times = [
            request.deadline,
            snapshot.trigger.expires_at,
            advice.expires_at,
            snapshot.market.latest_quote_at + timedelta(seconds=5),
            snapshot.features.as_of + timedelta(seconds=60),
            snapshot.account.as_of + timedelta(seconds=60),
        ]
        if advice.assessment.quantity is not None:
            if snapshot.market.book_as_of is None:
                return self.completed_at
            times.append(snapshot.market.book_as_of + timedelta(seconds=5))
        if advice.assessment.action == "buy" and snapshot.limits.max_daily_loss_usd is not None:
            context = self.risk_context
            if not context.daily_loss_complete or context.daily_loss_as_of is None:
                return self.completed_at
            times.append(context.daily_loss_as_of + timedelta(seconds=60))
            from zoneinfo import ZoneInfo

            local = self.completed_at.astimezone(ZoneInfo("Asia/Shanghai"))
            midnight = (local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            times.append(midnight)
        return min(times)


class DecisionReadRecord(DomainModel):
    """The committed completion, rather than the caller's pre-admission candidate."""

    request: DecisionRequest
    completion: DecisionCompletion | None = None
    current_recommendation: Recommendation | None = None

    @model_validator(mode="after")
    def matching_completion(self) -> Self:
        if self.completion is not None and (
            self.completion.request_id != self.request.request_id
            or self.completion.result.snapshot_id != self.request.snapshot.snapshot_id
        ):
            raise ValueError("read record does not match its claimed request")
        if self.current_recommendation is not None:
            original = self.completion.result.recommendation if self.completion else None
            if original is None or self.current_recommendation.model_dump(
                exclude={"status", "updated_at"}
            ) != original.model_dump(exclude={"status", "updated_at"}):
                raise ValueError("recommendation projection changed its immutable evidence")
        return self
