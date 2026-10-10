"""A human report remains human evidence after explicit exchange-ID verification."""

from hashlib import sha256
from typing import Literal, Self

from pydantic import model_validator

from .account import ObservedTrade, OrderSide
from .models import DomainModel, Identifier, LiveAccountRef, PositiveAmount, Revision, UtcDateTime


class UserReportedTrade(DomainModel):
    report_id: Identifier
    account_ref: LiveAccountRef
    symbol: Literal["BTCUSDT"] = "BTCUSDT"
    market_type: Literal["spot"] = "spot"
    side: OrderSide
    price: PositiveAmount
    quantity: PositiveAmount
    executed_at: UtcDateTime
    reported_at: UtcDateTime
    exchange_trade_id: Identifier | None = None
    source: Literal["user_reported"] = "user_reported"

    @model_validator(mode="after")
    def bounded_report(self) -> Self:
        if self.executed_at > self.reported_at:
            raise ValueError("reported execution cannot be in the future")
        if any(
            len(item) > 128
            for item in (self.report_id, self.account_ref, self.exchange_trade_id or "")
        ):
            raise ValueError("reported identifier exceeds its limit")
        for amount in (self.price, self.quantity):
            if len(amount.as_tuple().digits) > 128 or not -128 <= amount.as_tuple().exponent <= 128:
                raise ValueError("reported amount exceeds its bounded contract")
        return self

    @property
    def aggregate_id(self):
        return "user-report:" + sha256(self.report_id.encode()).hexdigest()


class ReportState(DomainModel):
    report: UserReportedTrade
    status: Literal["pending", "verified", "conflict"] = "pending"
    reasons: tuple[Identifier, ...] = ("not_checked",)
    matched_trade_id: Identifier | None = None
    checked_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def coherent_verification(self) -> Self:
        if (self.status == "verified") != (self.matched_trade_id is not None):
            raise ValueError("verified report requires its explicit exchange identity")
        if self.status == "verified" and (
            self.matched_trade_id != self.report.exchange_trade_id
            or self.reasons
            or self.checked_at is None
        ):
            raise ValueError("verification does not match the reported exchange identity")
        if self.status != "verified" and not self.reasons:
            raise ValueError("pending or conflicting report requires reasons")
        if self.checked_at is not None and self.checked_at < self.report.reported_at:
            raise ValueError("verification cannot predate the report")
        return self

    @property
    def aggregate_id(self):
        return self.report.aggregate_id

    @property
    def updated_at(self):
        return self.checked_at or self.report.reported_at


class ReportVerification(DomainModel):
    operation_id: Identifier
    expected_revision: Revision
    state: ReportState
    exchange_evidence: ObservedTrade | None = None

    @model_validator(mode="after")
    def bounded_operation(self) -> Self:
        if len(self.operation_id) > 128 or self.state.checked_at is None:
            raise ValueError("verification requires a bounded identity and check time")
        return self

    @property
    def aggregate_id(self):
        return "report-verification:" + sha256(self.operation_id.encode()).hexdigest()

    @property
    def updated_at(self):
        return self.state.updated_at


class ReportReceipt(DomainModel):
    state: ReportState
    revision: Revision
    event_id: Identifier
