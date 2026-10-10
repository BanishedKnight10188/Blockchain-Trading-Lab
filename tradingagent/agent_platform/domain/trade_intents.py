"""A proposed order with immutable event evidence; never an execution receipt."""

from typing import Literal, Self

from pydantic import model_validator

from .agent_trade_evidence import EventAgentTradeEvidence
from .futures_values import Price, Quantity
from .models import DomainModel, Identifier
from .trading_scope import ExecutionScope


class TradeIntent(DomainModel):
    intent_id: Identifier
    scope: ExecutionScope
    action: Literal["open_long", "open_short", "reduce"]
    quantity: Quantity
    protective_stop_mark: Price | None = None
    evidence: EventAgentTradeEvidence

    @model_validator(mode="after")
    def bound_proposal(self) -> Self:
        e = self.evidence
        if (self.intent_id, self.scope, self.action, self.quantity) != (
            e.intent_id,
            e.scope,
            e.action,
            e.quantity,
        ):
            raise ValueError("intent evidence mismatch")
        if self.action != "reduce":
            stop = self.protective_stop_mark
            if (
                stop is None
                or (self.action == "open_long" and stop >= e.original_quote.mark)
                or (self.action == "open_short" and stop <= e.original_quote.mark)
            ):
                raise ValueError("entry requires a protective stop on the correct side")
        elif self.protective_stop_mark is not None:
            raise ValueError("reduction cannot redefine protection")
        return self


class TradePreview(DomainModel):
    intent: TradeIntent
    allowed: bool
    reason: str | None = None
