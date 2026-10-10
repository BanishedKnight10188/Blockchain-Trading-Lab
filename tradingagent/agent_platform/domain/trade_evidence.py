"""The exact model question and bound answer that authorized a trade."""

from typing import Literal

from pydantic import Field, model_validator

from .decision_models import DecisionModelRequest, DecisionModelResponse
from .models import DomainModel
from .trading_plans import FuturesTradePlan


class TradeDecisionEvidence(DomainModel):
    decision_source: Literal["offline_mock", "real_jev"]
    request: DecisionModelRequest
    response: DecisionModelResponse
    plan: FuturesTradePlan | None = None
    elapsed_ms: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def bound_answer(self):
        self.response.bind_to(self.request)
        if self.plan is not None and self.response.answers[0].choice != self.plan.candidate_id:
            raise ValueError("trade evidence has a different selected plan")
        return self
