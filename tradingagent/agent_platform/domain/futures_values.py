"""Neutral exact futures observations, independent of account or execution target."""

from datetime import timedelta
from decimal import Context, Decimal
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from .common import decimal_value
from .models import DomainModel, UtcDateTime
from .session_market import FuturesSymbol


def bounded_decimal(value):
    result = decimal_value(value)
    parts = result.as_tuple()
    digits = list(parts.digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
    if len(digits) > 60 or not -24 <= parts.exponent <= 24 or result.copy_abs() > Decimal("1e24"):
        raise ValueError("futures decimal is outside arithmetic bounds")
    return result.normalize(Context(prec=80))


Amount = Annotated[Decimal, BeforeValidator(bounded_decimal), Field(ge=0)]
SignedAmount = Annotated[Decimal, BeforeValidator(bounded_decimal)]
Price = Annotated[Decimal, BeforeValidator(bounded_decimal), Field(gt=0, le=Decimal("1e12"))]
Quantity = Annotated[Decimal, BeforeValidator(bounded_decimal), Field(gt=0, le=Decimal("1e9"))]
Source = Literal["offline_replay", "binance_futures_public"]
# Bounded uncertainty between exchange and locally synchronized clocks.
# Preserve both original timestamps; this is not a freshness extension.
FUTURES_CLOCK_UNCERTAINTY_MS = 50


class FuturesQuote(DomainModel):
    model_config = ConfigDict(revalidate_instances="always")
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    symbol: FuturesSymbol
    source: Source
    bid: Price
    ask: Price
    mark: Price
    book_at: UtcDateTime
    mark_at: UtcDateTime
    received_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_evidence(self) -> Self:
        uncertainty = timedelta(
            milliseconds=FUTURES_CLOCK_UNCERTAINTY_MS
            if self.source == "binance_futures_public"
            else 0
        )
        if self.bid > self.ask or max(self.book_at, self.mark_at) > self.received_at + uncertainty:
            raise ValueError("book or evidence chronology is invalid")
        return self


class FuturesFunding(DomainModel):
    model_config = ConfigDict(revalidate_instances="always")
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    symbol: FuturesSymbol
    source: Source
    rate: SignedAmount = Field(ge=Decimal("-0.1"), le=Decimal("0.1"))
    mark: Price
    settled_at: UtcDateTime
    rate_type: Literal["regular"] = "regular"
