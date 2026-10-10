"""Explicit simulation facts in a namespace separate from actual exchange facts."""

from typing import Literal, Self

from pydantic import model_validator

from .account import OrderSide
from .models import (
    DomainModel,
    Identifier,
    NonnegativeAmount,
    PositiveAmount,
    UtcDateTime,
)


class PaperIntent(DomainModel):
    intent_id: Identifier
    mode: Literal["paper"]
    account_ref: Identifier
    symbol: Identifier
    side: OrderSide
    quantity: PositiveAmount
    limit_price: PositiveAmount | None = None
    created_at: UtcDateTime

    @model_validator(mode="after")
    def separate_account(self) -> Self:
        if not self.account_ref.startswith("paper:") or len(self.account_ref) == len("paper:"):
            raise ValueError("paper intent needs a separate paper account namespace")
        return self


class PaperFill(DomainModel):
    fill_id: Identifier
    intent_id: Identifier
    mode: Literal["paper"]
    account_ref: Identifier
    symbol: Identifier
    side: OrderSide
    quantity: PositiveAmount
    price: PositiveAmount
    fee: NonnegativeAmount
    fee_asset: Identifier
    slippage_bps: NonnegativeAmount
    model_version: Identifier
    filled_at: UtcDateTime

    @model_validator(mode="after")
    def separate_account(self) -> Self:
        if not self.account_ref.startswith("paper:") or len(self.account_ref) == len("paper:"):
            raise ValueError("paper fill needs a separate paper account namespace")
        return self
