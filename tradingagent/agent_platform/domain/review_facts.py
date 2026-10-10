"""Frozen FIFO allocations in quote currency, separate from retrospective prose."""

from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .account import CostStatus
from .common import MarketType
from .models import (
    DomainModel,
    FiniteDecimal,
    Identifier,
    LiveAccountRef,
    NonnegativeAmount,
    UtcDateTime,
)


class InventoryCoverage(DomainModel):
    account_ref: LiveAccountRef
    symbol: Identifier
    market_type: MarketType = MarketType.SPOT
    evidence_id: Identifier
    period_start: UtcDateTime
    as_of: UtcDateTime
    opening_base_quantity: NonnegativeAmount
    ending_base_quantity: NonnegativeAmount
    movements_complete: StrictBool = False
    nontrade_base_movements_abs: NonnegativeAmount | None = None

    @model_validator(mode="after")
    def coherent_period(self) -> Self:
        if self.period_start > self.as_of or len(self.evidence_id) > 128:
            raise ValueError("inventory coverage period and identity must be bounded")
        return self


class FifoMatch(DomainModel):
    buy_trade_id: Identifier | None
    sell_trade_id: Identifier
    inventory_quantity: NonnegativeAmount
    cost_quote: NonnegativeAmount | None
    proceeds_quote: NonnegativeAmount | None
    realized_pnl_quote: FiniteDecimal | None = None


class FifoLot(DomainModel):
    buy_trade_id: Identifier | None
    quantity: NonnegativeAmount
    cost_quote: NonnegativeAmount | None


class FifoResult(DomainModel):
    account_ref: LiveAccountRef
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    quote_asset: Literal["USDT"] = "USDT"
    data_cutoff: UtcDateTime
    algorithm_version: Literal["spot-fifo-v1"] = "spot-fifo-v1"
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cost_status: CostStatus
    reasons: tuple[Identifier, ...]
    matches: tuple[FifoMatch, ...] = Field(max_length=8192)
    remaining_lots: tuple[FifoLot, ...] = Field(max_length=4097)
    inventory_quantity: NonnegativeAmount
    unmatched_sold_quantity: NonnegativeAmount
    realized_pnl_quote: FiniteDecimal | None = None
    realized_pnl_usd: Literal[None] = None

    @model_validator(mode="after")
    def honest_valuation(self) -> Self:
        if self.cost_status != CostStatus.KNOWN and (
            self.realized_pnl_quote is not None
            or any(item.realized_pnl_quote is not None for item in self.matches)
        ):
            raise ValueError("incomplete basis cannot claim realized profit")
        return self
