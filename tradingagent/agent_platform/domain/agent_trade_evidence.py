"""Execution provenance for event and independent protective commands."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .futures_values import FuturesQuote, Quantity
from .models import DomainModel, Identifier, Revision
from .trading_scope import ExecutionScope


class _Evidence(DomainModel):
    intent_id: Identifier
    lane_id: Identifier
    scope: ExecutionScope
    action: Literal["open_long", "open_short", "reduce"]
    quantity: Quantity
    target_leverage: int | None = Field(default=None, strict=True, ge=1, le=20)
    account_revision: Revision
    lane_revision: Revision
    style_revision: Revision
    agent_revision: Revision
    policy_revision: Revision
    original_quote: FuturesQuote

    @model_validator(mode="after")
    def quote_scope(self) -> Self:
        if self.original_quote.symbol != self.scope.symbol or self.scope.environment != "paper":
            raise ValueError("event evidence requires its own Paper quote")
        return self


class EventAgentTradeEvidence(_Evidence):
    kind: Literal["event_agent"] = "event_agent"
    run_id: Identifier
    event_id: Identifier
    watch_id: Identifier
    definition_revision: Revision
    rule_hash: Identifier
    context_hash: Identifier
    tool_call_id: Identifier
    model_response_hash: Identifier
    tool_arguments_hash: Identifier


class GuardianTradeEvidence(_Evidence):
    kind: Literal["guardian"] = "guardian"
    protection_id: Identifier
    action: Literal["reduce"] = "reduce"
    target_leverage: None = None
