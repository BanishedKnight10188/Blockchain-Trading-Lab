"""Confirmed run policy and bounded, durable decision facts for any futures backend."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from .costs import ModelUsage
from .decision_models import DecisionModelRequest, DecisionModelResponse
from .futures_values import Amount, FuturesQuote
from .model_diagnostics import ModelDiagnostic
from .models import DomainModel, Identifier, Revision, UtcDateTime
from .trading_execution import ExecutionScope
from .trading_plans import FuturesTradePlan

PercentChoice = Annotated[int, Field(strict=True, ge=1, le=100)]
LeverageChoice = Annotated[int, Field(strict=True, ge=1, le=20)]


class TradingPolicy(DomainModel):
    model_config = ConfigDict(revalidate_instances="always")
    order_notional_usdt: Amount = Field(gt=0, le=Decimal("1e12"))
    max_price_drift_bps: Amount = Field(le=1000)
    min_confidence: Amount = Field(le=1)
    strategy_instructions: str = Field(strict=True, min_length=1, max_length=2048)
    decision_mode: Literal["fixed_notional", "parameterized"] = "fixed_notional"
    entry_margin_percents: tuple[PercentChoice, ...] = Field(
        default=(5, 10, 20), min_length=1, max_length=4
    )
    position_change_percents: tuple[PercentChoice, ...] = Field(
        default=(10, 20, 50), min_length=1, max_length=4
    )
    leverage_choices: tuple[LeverageChoice, ...] = Field(
        default=(1, 2, 5, 10), min_length=1, max_length=4
    )

    @model_validator(mode="after")
    def meaningful_strategy(self):
        if not self.strategy_instructions.strip():
            raise ValueError("strategy is required")
        for choices in (
            self.entry_margin_percents,
            self.position_change_percents,
            self.leverage_choices,
        ):
            if len(set(choices)) != len(choices):
                raise ValueError("parameter choices must be unique")
        return self


class TradingLimits(DomainModel):
    initial_usdt: Amount = Field(gt=0, le=Decimal("1e12"))
    leverage: int = Field(strict=True, ge=1, le=20)
    max_leverage: int | None = Field(default=None, strict=True, ge=1, le=20)
    max_position_notional: Amount = Field(gt=0, le=Decimal("1e12"))
    max_run_loss_usdt: Amount = Field(gt=0)
    fee_bps: Amount = Field(lt=10000)
    slippage_bps: Amount = Field(lt=10000)

    @model_validator(mode="after")
    def bounded_loss(self):
        if self.max_run_loss_usdt > self.initial_usdt:
            raise ValueError("loss budget cannot exceed funds")
        if self.max_leverage is not None and self.leverage > self.max_leverage:
            raise ValueError("initial leverage exceeds hard ceiling")
        return self


class TradingRun(DomainModel):
    scope: ExecutionScope
    policy: TradingPolicy
    limits: TradingLimits
    qty_step: Amount = Field(gt=0)
    min_qty: Amount = Field(gt=0)
    max_qty: Amount = Field(gt=0, le=Decimal("1e9"))
    min_notional: Amount = Field(gt=0)
    market_source: Literal["offline_replay", "binance_futures_public"]
    decision_source: Literal["offline_mock", "real_jev"]
    created_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_filters(self):
        if max(self.qty_step, self.min_qty) > self.max_qty:
            raise ValueError("configured order filters are inconsistent")
        return self


class FundingCheckpoint(DomainModel):
    account_ref: Identifier
    cursor: UtcDateTime
    next_due: UtcDateTime | None = None

    @model_validator(mode="after")
    def pending_schedule(self):
        if self.next_due is not None and self.next_due <= self.cursor:
            raise ValueError("unprocessed funding must be after the saved cursor")
        return self


class TradingCycle(DomainModel):
    request_id: Identifier
    scope: ExecutionScope
    style_revision: Revision
    trader_revision: Revision
    account_revision: Revision
    created_at: UtcDateTime
    completed_at: UtcDateTime | None = None
    status: Literal[
        "pending", "wait", "advised", "filled", "rejected", "unknown", "interrupted"
    ] = "pending"
    decision: Literal["OPEN_LONG", "OPEN_SHORT", "REDUCE", "WAIT"] | None = None
    confidence: Amount | None = Field(default=None, le=1)
    reason: str | None = Field(default=None, max_length=64)
    usage: ModelUsage | None = None
    command_id: Identifier | None = None
    quote: FuturesQuote | None = None
    plan: FuturesTradePlan | None = None
    diagnostic: ModelDiagnostic | None = None
    model_request: DecisionModelRequest | None = None
    model_response: DecisionModelResponse | None = None
    model_latency_ms: int | None = Field(default=None, strict=True, ge=0)

    def public_summary(self):
        value = self.model_dump(mode="json", exclude={"model_request", "model_response"})
        if self.model_response and self.model_response.local_timing is not None:
            value["local_timing"] = self.model_response.local_timing.model_dump(mode="json")
        if self.model_response and self.model_response.transport_evidence is not None:
            value["transport_evidence"] = self.model_response.transport_evidence.model_dump(
                mode="json"
            )
        value["choice_probability_adjustments"] = (
            [
                a.model_dump(mode="json", exclude={"original_probabilities"})
                for a in self.model_response.choice_probability_adjustments
            ]
            if self.model_response
            else []
        )
        return value

    @model_validator(mode="after")
    def consistent_cycle(self):
        if (self.status == "pending") != (self.completed_at is None):
            raise ValueError("cycle status and completion disagree")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("cycle time cannot regress")
        if self.quote is not None and (
            self.quote.symbol != self.scope.symbol or self.quote.received_at > self.created_at
        ):
            raise ValueError("cycle quote has a different identity or future reception")
        if self.usage is not None and self.usage.request_id != self.request_id:
            raise ValueError("cycle fee has another request identity")
        if self.model_request is not None and (
            self.model_request.request_id != self.request_id
            or self.model_request.captured_at != self.created_at
        ):
            raise ValueError("cycle request has another identity or time")
        if self.model_response is not None:
            if self.model_request is None:
                raise ValueError("cycle response requires its original request")
            self.model_response.bind_to(self.model_request)
        return self
