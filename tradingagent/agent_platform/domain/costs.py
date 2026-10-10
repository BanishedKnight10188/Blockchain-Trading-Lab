"""Exact cost and reservation contracts; durable enforcement belongs in a store."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, StrictBool, model_serializer, model_validator

from .common import DecisionPurpose, exact_add
from .models import DomainModel, Identifier, NonnegativeAmount, UtcDateTime


class RouteKind(StrEnum):
    RULE = "rule"
    ECONOMY = "economy"
    STANDARD = "standard"
    REVIEW = "review"


class RouteDecision(DomainModel):
    route_id: Identifier
    kind: RouteKind
    purpose: DecisionPurpose
    reason: Identifier
    model_version: Identifier | None = None
    price_version: Identifier | None = None
    paid: StrictBool = False

    @model_validator(mode="after")
    def known_paid_route(self) -> Self:
        if self.paid and (
            self.kind == RouteKind.RULE or self.model_version is None or self.price_version is None
        ):
            raise ValueError("paid route needs an explicit model and verified price version")
        return self


class BudgetRequest(DomainModel):
    request_id: Identifier
    route_id: Identifier
    purpose: DecisionPurpose
    price_version: Identifier
    estimated_cost_usd: NonnegativeAmount
    daily_limit_usd: NonnegativeAmount
    hourly_call_limit: int = Field(default=60, strict=True, ge=0)
    requested_at: UtcDateTime
    budget_timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    provider_managed: StrictBool = False
    session_id: Identifier | None = None

    @model_serializer(mode="wrap")
    def preserve_old_request(self, handler):
        value = handler(self)
        if not self.provider_managed:
            value.pop("provider_managed", None)
        if self.session_id is None:
            value.pop("session_id", None)
        return value

    @property
    def budget_day(self) -> date:
        return self.requested_at.astimezone(ZoneInfo(self.budget_timezone)).date()

    @property
    def within_single_request_limit(self) -> bool:
        return self.estimated_cost_usd <= self.daily_limit_usd and self.hourly_call_limit > 0


class BillingStatus(StrEnum):
    UNKNOWN = "unknown"
    ESTIMATED = "estimated"
    CONFIRMED = "confirmed"


class ModelUsage(DomainModel):
    request_id: Identifier
    route_id: Identifier
    model_version: Identifier
    input_tokens: int = Field(strict=True, ge=0)
    output_tokens: int = Field(strict=True, ge=0)
    token_counts_known: StrictBool = True
    estimated_cost_usd: NonnegativeAmount
    actual_cost_usd: NonnegativeAmount | None = None
    billing_status: BillingStatus
    recorded_at: UtcDateTime

    @model_validator(mode="after")
    def honest_billing(self) -> Self:
        if not self.token_counts_known and (self.input_tokens != 0 or self.output_tokens != 0):
            raise ValueError("unknown token counts cannot contain measurements")
        if (self.billing_status == BillingStatus.CONFIRMED) != (self.actual_cost_usd is not None):
            raise ValueError("only confirmed billing may contain an actual cost")
        return self


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    UNKNOWN = "unknown"
    SETTLED = "settled"
    RELEASED = "released"


class BudgetReservation(DomainModel):
    reservation_id: Identifier
    request: BudgetRequest
    status: ReservationStatus = ReservationStatus.RESERVED
    updated_at: UtcDateTime
    actual_cost_usd: NonnegativeAmount | None = None

    @model_validator(mode="after")
    def honest_reservation(self) -> Self:
        if self.updated_at < self.request.requested_at:
            raise ValueError("reservation cannot predate its request")
        if (self.status == ReservationStatus.SETTLED) != (self.actual_cost_usd is not None):
            raise ValueError("settled reservation requires the actual cost")
        return self

    @property
    def held_cost_usd(self) -> Decimal:
        if self.status in (ReservationStatus.RESERVED, ReservationStatus.UNKNOWN):
            return self.request.estimated_cost_usd
        return Decimal(0)


class BudgetBalance(DomainModel):
    budget_day: date
    daily_limit_usd: NonnegativeAmount
    spent_usd: NonnegativeAmount
    reserved_usd: NonnegativeAmount
    hourly_call_count: int = Field(strict=True, ge=0)
    billing_frozen: StrictBool = False
    provider_managed: StrictBool = False

    @model_serializer(mode="wrap")
    def preserve_old_balance(self, handler):
        value = handler(self)
        if not self.provider_managed:
            value.pop("provider_managed", None)
        return value

    @model_validator(mode="after")
    def visible_overrun(self) -> Self:
        exposure = exact_add(self.spent_usd, self.reserved_usd)
        if (
            not self.provider_managed
            and exposure > self.daily_limit_usd
            and not self.billing_frozen
        ):
            raise ValueError("budget overrun must freeze further paid calls")
        return self
