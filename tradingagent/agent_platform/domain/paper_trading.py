"""Owned virtual funds and bounded policy, separate from exchange account facts."""

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from .common import decimal_value
from .costs import ModelUsage
from .decision_models import Probability
from .models import DomainModel, Identifier, Revision, UtcDateTime
from .paper import PaperFill
from .sessions import TradingStyle


def paper_amount(value):
    amount = decimal_value(value)
    if (
        amount < 0
        or len(amount.as_tuple().digits) > 34
        or not -32 <= amount.as_tuple().exponent <= 32
    ):
        raise ValueError("paper amounts must be nonnegative and bounded exact decimals")
    return amount


PaperAmount = Annotated[Decimal, BeforeValidator(paper_amount)]


class PaperSettings(DomainModel):
    initial_usdt: PaperAmount = Field(gt=0, le=Decimal("1e12"))
    order_quantity: PaperAmount = Field(gt=0, le=Decimal("1e6"))
    max_position_quantity: PaperAmount = Field(gt=0, le=Decimal("1e6"))
    max_run_loss_usdt: PaperAmount = Field(gt=0)
    fee_bps: PaperAmount = Field(lt=10000)
    slippage_bps: PaperAmount = Field(lt=10000)
    max_price_drift_bps: PaperAmount = Field(gt=0, lt=10000)
    min_confidence: Probability
    strategy_instructions: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def coherent_limits(self) -> Self:
        if not self.strategy_instructions.strip():
            raise ValueError("strategy must be explicitly specified")
        if self.order_quantity > self.max_position_quantity:
            raise ValueError("order quantity exceeds maximum position")
        if self.max_run_loss_usdt > self.initial_usdt:
            raise ValueError("run loss limit exceeds initial virtual funds")
        # Leave arithmetic headroom for price * quantity, fees and balances.
        for name in type(self).model_fields:
            amount = getattr(self, name)
            if isinstance(amount, Decimal) and len(amount.as_tuple().digits) > 20:
                raise ValueError("policy decimal precision is bounded")
        return self


class PaperAccountState(DomainModel):
    account_ref: Identifier
    session_id: Identifier
    revision: Revision = 1
    settings: PaperSettings
    usdt: PaperAmount
    btc: PaperAmount
    status: Literal["paused", "running"] = "paused"
    pending_request_id: Identifier | None = None
    activation_revision: Revision = 1
    decision_source: Literal["offline_mock", "real_jev"] = "offline_mock"
    market_source: Literal["offline_demo", "binance_public"] = "offline_demo"
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_account(self) -> Self:
        if self.account_ref != "paper:" + self.session_id:
            raise ValueError("paper funds must belong to their own session")
        if self.updated_at < self.created_at:
            raise ValueError("paper chronology cannot move backwards")
        if self.status == "paused" and self.pending_request_id is not None:
            raise ValueError("paused account cannot own pending work")
        if self.revision == 1 and (self.usdt != self.settings.initial_usdt or self.btc != 0):
            raise ValueError(
                "initial wallet must use explicitly confirmed virtual USDT and zero BTC"
            )
        return self


class PaperCycleState(DomainModel):
    aggregate_id: Identifier
    account_ref: Identifier
    session_id: Identifier
    request_id: Identifier
    revision: Revision = 1
    activation_revision: Revision
    trader_revision: Revision
    style_revision: Revision
    style: TradingStyle
    status: Literal["pending", "filled", "wait", "rejected", "unavailable", "discarded"] = "pending"
    decision: Literal["BUY", "SELL", "WAIT"] | None = None
    confidence: Probability | None = None
    fill: PaperFill | None = None
    usage: ModelUsage | None = None
    reason: Identifier | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    deadline: UtcDateTime
    response_recorded: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def coherent_cycle(self) -> Self:
        if (
            self.account_ref != "paper:" + self.session_id
            or self.aggregate_id != self.account_ref + ":cycle:" + self.request_id
        ):
            raise ValueError("cycle identity must belong to the paper account")
        if not self.created_at < self.deadline <= self.created_at + timedelta(seconds=15):
            raise ValueError("cycle deadline must be within fifteen seconds")
        if self.updated_at < self.created_at:
            raise ValueError("cycle chronology cannot move backwards")
        if self.usage is not None and self.usage.request_id != self.request_id:
            raise ValueError("usage must belong to the same request")
        if (self.status == "filled") != (self.fill is not None):
            raise ValueError("only filled cycles may carry virtual fills")
        if self.fill is not None and (
            self.fill.account_ref != self.account_ref or self.fill.intent_id != self.request_id
        ):
            raise ValueError("fill must belong to the claimed request")
        return self
