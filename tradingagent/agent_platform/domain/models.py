"""Validated owned values; external transport dictionaries stay in adapters."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    WrapValidator,
)

from .common import (
    decimal_value,
    live_account_ref,
    nonnegative_amount,
    positive_amount,
    required_identifier,
    utc_datetime,
)


def _utc_value(value: Any, handler: Callable, info: ValidationInfo) -> datetime:
    if info.mode == "json":
        value = handler(value)
    return handler(utc_datetime(value))


Identifier = Annotated[str, BeforeValidator(required_identifier)]
LiveAccountRef = Annotated[str, BeforeValidator(live_account_ref)]
FiniteDecimal = Annotated[Decimal, BeforeValidator(decimal_value)]
PositiveAmount = Annotated[Decimal, BeforeValidator(positive_amount)]
NonnegativeAmount = Annotated[Decimal, BeforeValidator(nonnegative_amount)]
UtcDateTime = Annotated[datetime, WrapValidator(_utc_value)]
Revision = Annotated[int, Field(strict=True, ge=1)]
StyleStrength = Annotated[int, Field(strict=True, ge=0, le=100)]


class DomainModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        hide_input_in_errors=True,
    )
    schema_version: Literal[1] = 1
