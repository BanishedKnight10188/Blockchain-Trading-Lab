"""Immutable execution and account facts shared by futures backends."""

import json
from datetime import timedelta
from decimal import Context, Decimal, localcontext
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .agent_trade_evidence import EventAgentTradeEvidence, GuardianTradeEvidence
from .futures_values import Amount, FuturesQuote, Price, Quantity, SignedAmount
from .models import DomainModel, Identifier, Revision, UtcDateTime
from .trade_evidence import TradeDecisionEvidence
from .trading_scope import ExecutionScope as ExecutionScope


class _ExecutionFact(DomainModel):
    model_config = ConfigDict(revalidate_instances="always")


class TradeCommand(_ExecutionFact):
    command_id: Identifier = Field(max_length=128)
    scope: ExecutionScope
    action: Literal["open_long", "open_short", "reduce"]
    quantity: Quantity
    target_leverage: int | None = Field(default=None, strict=True, ge=1, le=20)
    created_at: UtcDateTime
    expires_at: UtcDateTime
    expected_account_revision: Revision
    style_revision: Revision
    trader_revision: Revision
    decision_evidence: (
        TradeDecisionEvidence | EventAgentTradeEvidence | GuardianTradeEvidence | None
    ) = None

    @model_validator(mode="after")
    def bounded_lifetime(self) -> Self:
        if not timedelta(0) < self.expires_at - self.created_at <= timedelta(seconds=30):
            raise ValueError("execution command requires a bounded positive lifetime")
        if self.action == "reduce" and self.target_leverage is not None:
            raise ValueError("reduction must not change leverage")
        evidence = self.decision_evidence
        if self.command_id.startswith(("agent:", "guardian:")) and not isinstance(
            evidence, (EventAgentTradeEvidence, GuardianTradeEvidence)
        ):
            raise ValueError("event commands require typed evidence")
        if isinstance(evidence, (EventAgentTradeEvidence, GuardianTradeEvidence)):
            prefix = "agent:" if isinstance(evidence, EventAgentTradeEvidence) else "guardian:"
            if (
                self.command_id != prefix + evidence.intent_id
                or self.scope != evidence.scope
                or self.action != evidence.action
                or self.quantity != evidence.quantity
                or self.target_leverage != evidence.target_leverage
                or self.expected_account_revision != evidence.account_revision
                or self.style_revision != evidence.style_revision
                or self.trader_revision != evidence.agent_revision
            ):
                raise ValueError("event evidence differs from the executed command")
            return self
        if self.decision_evidence is not None:
            evidence = self.decision_evidence
            state = json.loads(evidence.request.state_json)
            account = state.get("account", {})
            if (
                self.command_id != "jev:" + evidence.request.request_id
                or state.get("symbol") != self.scope.symbol
                or state.get("environment") != self.scope.environment
                or account.get("scope") != self.scope.model_dump(mode="json")
                or account.get("revision") != self.expected_account_revision
                or state.get("style_revision") != self.style_revision
                or state.get("trader_revision") != self.trader_revision
                or not evidence.request.captured_at <= self.created_at < self.expires_at
                or self.expires_at > evidence.request.deadline
            ):
                raise ValueError("trade evidence does not belong to this command")
            if evidence.plan is not None and (
                evidence.plan.action != self.action
                or evidence.plan.quantity != self.quantity
                or (self.action != "reduce" and evidence.plan.leverage != self.target_leverage)
            ):
                raise ValueError("trade evidence differs from the executed plan")
        return self


ExecutionStatus = Literal[
    "pending", "accepted", "partially_filled", "filled", "rejected", "canceled", "unknown"
]
TERMINAL_STATUSES = frozenset({"filled", "rejected", "canceled"})


class ExecutionReceipt(_ExecutionFact):
    command: TradeCommand
    status: ExecutionStatus
    filled_quantity: Amount = Decimal(0)
    average_price: Price | None = None
    fee_usdt: Amount = Decimal(0)
    backend_order_id: Identifier | None = None
    backend_at: UtcDateTime | None = None
    observed_at: UtcDateTime
    reason: (
        Literal[
            "preflight_rejected",
            "execution_unknown",
            "market_unavailable",
            "account_unavailable",
            "not_found",
            "account_liquidated",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def consistent_fill(self) -> Self:
        q = self.filled_quantity
        if q > self.command.quantity or self.observed_at < self.command.created_at:
            raise ValueError("execution quantities or chronology are inconsistent")
        if self.backend_at is not None and not (
            self.command.created_at <= self.backend_at <= self.observed_at
        ):
            raise ValueError("backend event is outside the observed command chronology")
        if q > 0 and self.backend_at is None:
            raise ValueError("actual execution requires its backend event time")
        if (q > 0) != (self.average_price is not None) or (q == 0 and self.fee_usdt != 0):
            raise ValueError("price and fees require actual execution")
        if self.status in ("pending", "accepted", "rejected") and q:
            raise ValueError("unfilled state cannot contain execution")
        if self.status == "filled" and q != self.command.quantity:
            raise ValueError("filled state requires the entire requested quantity")
        if self.status == "partially_filled" and not 0 < q < self.command.quantity:
            raise ValueError("partial state requires an incomplete positive execution")
        if self.status in ("unknown", "canceled") and q == self.command.quantity:
            raise ValueError("complete execution must be reported as filled")
        return self


class TradingAccountSnapshot(_ExecutionFact):
    scope: ExecutionScope
    revision: Revision
    status: Literal["paused", "running", "liquidated"]
    free_usdt: Amount
    margin_usdt: Amount
    quantity: Amount = Field(le=Decimal("1e9"))
    side: Literal["long", "short"] | None
    entry_notional: Amount
    realized_pnl_usdt: SignedAmount
    funding_usdt: SignedAmount
    fees_usdt: Amount
    equity_usdt: SignedAmount | None = None
    unrealized_pnl_usdt: SignedAmount | None = None
    quote: FuturesQuote | None = None
    captured_at: UtcDateTime
    leverage: int | None = Field(default=None, strict=True, ge=1, le=20)

    @model_validator(mode="after")
    def consistent_position(self) -> Self:
        if (self.quantity == 0) != (self.side is None):
            raise ValueError("position direction and quantity disagree")
        if self.quantity == 0 and (self.margin_usdt or self.entry_notional):
            raise ValueError("flat account cannot retain margin or entry cost")
        if self.quantity > 0 and self.entry_notional <= 0:
            raise ValueError("open position needs entry cost")
        if self.status == "liquidated" and self.quantity:
            raise ValueError("liquidated account cannot hold a position")
        if self.quote is not None and (
            self.quote.symbol != self.scope.symbol or self.quote.received_at > self.captured_at
        ):
            raise ValueError("account quote has wrong scope or future reception")
        if (self.equity_usdt is None) != (self.unrealized_pnl_usdt is None):
            raise ValueError("valuation components must be available together")
        if self.equity_usdt is not None:
            if self.quote is None:
                raise ValueError("valuation requires sourced market evidence")
            with localcontext(Context(prec=80)):
                if self.equity_usdt != self.free_usdt + self.margin_usdt + self.unrealized_pnl_usdt:
                    raise ValueError("valuation does not reconcile")
        return self


class ExecutionRecord(_ExecutionFact):
    command: TradeCommand
    receipt: ExecutionReceipt
    revision: Revision = 1
    quote: FuturesQuote | None = None

    @model_validator(mode="after")
    def matching_facts(self) -> Self:
        if self.command != self.receipt.command:
            raise ValueError("execution record belongs to another command")
        if self.quote is not None and self.quote.symbol != self.command.scope.symbol:
            raise ValueError("execution quote belongs to another contract")
        return self
