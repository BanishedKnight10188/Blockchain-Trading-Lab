"""Owned USDT one-way isolated simulation values, independent of Spot and providers."""

from decimal import Context, Decimal, localcontext
from typing import Literal, Self

from pydantic import Field, model_validator

from .futures_values import Amount as Amount
from .futures_values import FuturesFunding as FuturesPaperFunding
from .futures_values import FuturesQuote as FuturesPaperQuote
from .futures_values import Price as Price
from .futures_values import Quantity as Quantity
from .futures_values import SignedAmount as SignedAmount
from .futures_values import Source as Source
from .futures_values import bounded_decimal as bounded_decimal
from .models import DomainModel, Identifier, Revision, UtcDateTime
from .session_market import FuturesSymbol
from .trading_execution import TradeCommand


class FuturesPaperSettings(DomainModel):
    initial_usdt: Amount = Field(gt=0, le=Decimal("1e12"))
    leverage: int = Field(strict=True, ge=1, le=20)
    max_leverage: int | None = Field(default=None, strict=True, ge=1, le=20)
    max_position_notional: Amount = Field(gt=0, le=Decimal("1e12"))
    max_run_loss_usdt: Amount = Field(gt=0)
    fee_bps: Amount = Field(lt=10000)
    slippage_bps: Amount = Field(lt=10000)

    @model_validator(mode="after")
    def loss_bound(self) -> Self:
        if self.max_run_loss_usdt > self.initial_usdt:
            raise ValueError("loss budget cannot exceed virtual funds")
        if self.max_leverage is not None and self.leverage > self.max_leverage:
            raise ValueError("leverage exceeds confirmed ceiling")
        return self


class FuturesPaperRules(DomainModel):
    source: Literal["simulation_fixed"] = "simulation_fixed"
    qty_step: Quantity
    min_qty: Quantity
    max_qty: Quantity
    tick_size: Price
    min_notional: Amount
    maintenance_margin_rate: Amount = Field(gt=0, lt=1)
    liquidation_fee_bps: Amount = Field(lt=10000)

    @model_validator(mode="after")
    def quantity_bounds(self) -> Self:
        if self.max_qty < self.min_qty or self.qty_step > self.max_qty:
            raise ValueError("quantity rules are inconsistent")
        return self


class FuturesPaperState(DomainModel):
    account_ref: Identifier
    session_id: Identifier
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    currency: Literal["USDT"] = "USDT"
    symbol: FuturesSymbol
    revision: Revision = 1
    status: Literal["paused", "running", "liquidated"] = "paused"
    settings: FuturesPaperSettings
    rules: FuturesPaperRules
    free_usdt: Amount
    margin_usdt: Amount = Decimal(0)
    quantity: Amount = Field(default=Decimal(0), le=Decimal("1e9"))
    side: Literal["long", "short"] | None = None
    entry_notional: Amount = Decimal(0)
    realized_pnl_usdt: SignedAmount = Decimal(0)
    funding_usdt: SignedAmount = Decimal(0)
    fees_usdt: Amount = Decimal(0)
    shortfall_usdt: Amount = Decimal(0)
    last_funding_at: UtcDateTime | None = None
    position_updated_at: UtcDateTime | None = None
    last_quote: "FuturesPaperQuote | None" = None
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_wallet(self) -> Self:
        if self.account_ref != "paper:futures:" + self.session_id:
            raise ValueError("simulation identity must belong to this futures session")
        if self.last_quote is not None and self.last_quote.symbol != self.symbol:
            raise ValueError("quote watermark has a different contract identity")
        if (
            self.updated_at < self.created_at
            or (self.last_funding_at is not None and self.last_funding_at > self.updated_at)
            or (
                self.position_updated_at is not None
                and not self.created_at <= self.position_updated_at <= self.updated_at
            )
        ):
            raise ValueError("wallet chronology is inconsistent")
        if (self.quantity == 0) != (self.side is None):
            raise ValueError("position direction and quantity disagree")
        if self.quantity == 0 and (self.margin_usdt or self.entry_notional):
            raise ValueError("flat position cannot retain margin or entry cost")
        if self.quantity > 0 and self.entry_notional <= 0:
            raise ValueError("open position needs a positive entry cost")
        if self.status == "liquidated" and self.quantity:
            raise ValueError("liquidated account cannot hold a position")
        with localcontext(Context(prec=80)):
            if self.rules.maintenance_margin_rate * self.settings.leverage >= 1:
                raise ValueError("maintenance rate must be below initial margin rate")
            cash = self.free_usdt + self.margin_usdt
            expected = (
                self.settings.initial_usdt
                + self.realized_pnl_usdt
                + self.funding_usdt
                - self.fees_usdt
            )
            if cash != expected:
                raise ValueError("virtual cash does not reconcile to its recorded flows")
        if self.revision == 1 and (
            self.status != "paused"
            or self.quantity
            or self.fees_usdt
            or self.funding_usdt
            or self.realized_pnl_usdt
            or self.shortfall_usdt
            or self.last_funding_at
        ):
            raise ValueError("initial simulation must be paused and unspent")
        return self


class FuturesPaperOrder(DomainModel):
    action: Literal["open_long", "open_short", "reduce"]
    quantity: Quantity
    target_leverage: int | None = Field(default=None, strict=True, ge=1, le=20)

    @model_validator(mode="after")
    def reduction_keeps_leverage(self):
        if self.action == "reduce" and self.target_leverage is not None:
            raise ValueError("reduction must not change leverage")
        return self


class FuturesPaperValuation(DomainModel):
    equity_usdt: SignedAmount
    isolated_equity_usdt: SignedAmount
    unrealized_pnl_usdt: SignedAmount
    maintenance_margin_usdt: Amount
    requires_liquidation: bool


class FuturesPaperOperation(DomainModel):
    kind: Literal["trade", "funding", "liquidation"]
    before_revision: Revision
    after_revision: Revision
    quantity: Amount = Decimal(0)
    price: Price | None = None
    side: Literal["buy", "sell"] | None = None
    realized_pnl_usdt: SignedAmount = Decimal(0)
    fee_usdt: Amount = Decimal(0)
    funding_usdt: SignedAmount = Decimal(0)
    shortfall_usdt: Amount = Decimal(0)
    reason: Literal["maintenance", "funding_exhausted"] | None = None
    occurred_at: UtcDateTime


class FuturesPaperTransition(DomainModel):
    state: FuturesPaperState
    operation: FuturesPaperOperation

    @model_validator(mode="after")
    def matching_revision(self) -> Self:
        if (
            self.operation.after_revision != self.state.revision
            or self.operation.before_revision + 1 != self.state.revision
            or self.operation.occurred_at != self.state.updated_at
        ):
            raise ValueError("operation and wallet version disagree")
        return self


class FuturesPaperRecord(DomainModel):
    command_id: Identifier
    account_ref: Identifier
    fingerprint: Identifier
    kind: Literal[
        "configure",
        "parameterize",
        "start",
        "pause",
        "recover",
        "mark",
        "trade",
        "funding",
        "liquidation",
    ]
    state: FuturesPaperState
    operation: FuturesPaperOperation | None = None
    quote: FuturesPaperQuote | None = None
    order: FuturesPaperOrder | None = None
    funding: FuturesPaperFunding | None = None
    execution_command: TradeCommand | None = None
    before_state: FuturesPaperState | None = None

    @model_validator(mode="after")
    def matching_identity(self) -> Self:
        if self.account_ref != self.state.account_ref:
            raise ValueError("record identity does not match its wallet")
        for evidence in (self.quote, self.funding):
            if evidence is not None and evidence.symbol != self.state.symbol:
                raise ValueError("record evidence has a different contract identity")
        if self.operation is not None:
            FuturesPaperTransition(state=self.state, operation=self.operation)
            if self.kind != self.operation.kind:
                raise ValueError("record kind does not match its operation")
        if self.before_state is not None and (
            self.operation is None
            or self.before_state.account_ref != self.account_ref
            or self.before_state.session_id != self.state.session_id
            or self.before_state.symbol != self.state.symbol
            or self.before_state.revision != self.operation.before_revision
            or self.before_state.updated_at > self.operation.occurred_at
        ):
            raise ValueError("record before-state does not match its operation")
        if self.execution_command is not None:
            command = self.execution_command
            if (
                command.command_id != self.command_id
                or command.scope.environment != "paper"
                or command.scope.account_ref != self.account_ref
                or command.scope.session_id != self.state.session_id
                or command.scope.symbol != self.state.symbol
                or self.order is None
                or self.order.action != command.action
                or self.order.quantity != command.quantity
                or self.order.target_leverage != command.target_leverage
                or self.operation is None
                or self.operation.before_revision != command.expected_account_revision
                or not command.created_at <= self.operation.occurred_at <= command.expires_at
            ):
                raise ValueError("execution command does not match its recorded operation")
        return self


FuturesPaperState.model_rebuild()
