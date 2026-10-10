"""Reduced advice projections contain no account scope or raw observation payloads."""

from typing import Literal

from pydantic import StrictBool

from .models import (
    DomainModel,
    Identifier,
    NonnegativeAmount,
    PositiveAmount,
    Revision,
    StyleStrength,
    UtcDateTime,
)


class UsageView(DomainModel):
    billing_status: Literal["confirmed", "unknown", "estimated"]
    estimated_cost_usd: NonnegativeAmount
    actual_cost_usd: NonnegativeAmount | None
    token_counts_known: StrictBool
    input_tokens: int | None
    output_tokens: int | None


class AdviceView(DomainModel):
    status: Literal[
        "unavailable", "pending", "published", "expired", "superseded", "accepted", "rejected"
    ]
    reasons: tuple[Identifier, ...] = ()
    recommendation_id: Identifier | None = None
    action: Literal["buy", "sell", "hold"] | None = None
    explanation: Identifier | None = None
    quantity: PositiveAmount | None = None
    original_author: Literal["agent"] | None = None
    source: Literal["rule", "fake", "model"] | None = None
    style_strength: StyleStrength | None = None
    style_revision: Revision | None = None
    policy_version: Literal["style-v1"] | None = None
    evidence_as_of: UtcDateTime | None = None
    created_at: UtcDateTime | None = None
    expires_at: UtcDateTime | None = None
    usage: UsageView | None = None
    usage_status: Literal["not_recorded", "recorded", "unavailable"] = "not_recorded"
    model_failure: Identifier | None = None
