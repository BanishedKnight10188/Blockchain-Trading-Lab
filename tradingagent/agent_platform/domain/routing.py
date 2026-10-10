"""Explicit configured tiers and versioned synthetic/verified price tables."""

from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .models import DomainModel, Identifier, NonnegativeAmount, UtcDateTime


def bounded_money(value) -> bool:
    return len(value.as_tuple().digits) <= 34 and -32 <= value.as_tuple().exponent <= 32


class ModelPrice(DomainModel):
    version: Identifier
    input_usd_per_million: NonnegativeAmount
    output_usd_per_million: NonnegativeAmount
    verified_at: UtcDateTime
    valid_until: UtcDateTime | None = None

    def available_at(self, at) -> bool:
        return self.verified_at <= at and (self.valid_until is None or at < self.valid_until)

    @model_validator(mode="after")
    def finite_price(self) -> Self:
        if (self.valid_until is not None and self.valid_until <= self.verified_at) or not all(
            bounded_money(value)
            for value in (self.input_usd_per_million, self.output_usd_per_million)
        ):
            raise ValueError("price validity and arithmetic bounds are inconsistent")
        return self


class ModelTier(DomainModel):
    kind: Literal["economy", "standard", "review"]
    model_version: Identifier
    price: ModelPrice | None = None
    max_output_tokens: int = Field(default=1024, strict=True, ge=1, le=8192)
    max_single_cost_usd: NonnegativeAmount = "0"
    prompt_overhead_tokens: int = Field(default=1024, strict=True, ge=0, le=65536)
    enabled: StrictBool = False

    @model_validator(mode="after")
    def finite_ceiling(self) -> Self:
        if not bounded_money(self.max_single_cost_usd):
            raise ValueError("single request cost ceiling is out of bounds")
        return self


class RoutingPolicy(DomainModel):
    routes: tuple[ModelTier, ...] = ()
    daily_limit_usd: NonnegativeAmount = "0"
    hourly_call_limit: int = Field(default=60, strict=True, ge=0, le=60)
    version: Identifier = "routing-v1"

    @model_validator(mode="after")
    def unique_tiers(self) -> Self:
        if len({route.kind for route in self.routes}) != len(self.routes):
            raise ValueError("routing tiers cannot be duplicated")
        if not bounded_money(self.daily_limit_usd):
            raise ValueError("daily cost ceiling is out of bounds")
        return self


class ModelCostQuote(DomainModel):
    route_id: Identifier
    price_version: Identifier
    input_token_upper_bound: int = Field(strict=True, ge=1, le=131072)
    max_output_tokens: int = Field(strict=True, ge=1, le=8192)
    estimated_cost_usd: NonnegativeAmount
