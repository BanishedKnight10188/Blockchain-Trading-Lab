"""Finite model requests and responses without credentials or transport objects."""

from typing import Self

from pydantic import Field, model_validator

from .common import DecisionPurpose
from .costs import ModelUsage, RouteDecision, RouteKind
from .decisions import AdvisoryAssessment, DecisionSnapshot
from .models import DomainModel, Identifier, UtcDateTime


class ModelRequest(DomainModel):
    request_id: Identifier
    snapshot: DecisionSnapshot
    purpose: DecisionPurpose
    route: RouteDecision
    deadline: UtcDateTime
    max_output_tokens: int = Field(strict=True, ge=1, le=8192)
    prompt_version: Identifier

    @model_validator(mode="after")
    def coherent_request(self) -> Self:
        if self.deadline <= self.snapshot.captured_at:
            raise ValueError("model deadline must follow snapshot capture")
        if self.route.purpose != self.purpose:
            raise ValueError("model route purpose does not match request")
        if self.route.kind == RouteKind.RULE or self.route.model_version is None:
            raise ValueError("model request requires an explicit model route")
        return self


class ProviderMetadata(DomainModel):
    request_id: str = Field(
        strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:/-]+$"
    )
    model_id: str = Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:/-]+$")
    provider: str = Field(strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9 ._:/-]+$")


class ModelResponse(DomainModel):
    request_id: Identifier
    assessment: AdvisoryAssessment
    usage: ModelUsage
    provider_metadata: ProviderMetadata | None = None

    @model_validator(mode="after")
    def coherent_response(self) -> Self:
        if self.request_id != self.usage.request_id:
            raise ValueError("model response usage belongs to another request")
        if self.assessment.source not in ("fake", "model"):
            raise ValueError("model response cannot claim a different author")
        return self
