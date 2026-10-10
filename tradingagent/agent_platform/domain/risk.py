"""User discipline and deterministic risk outcomes, independent of style or models."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import StrictBool, model_validator

from .market import Instrument
from .models import DomainModel, Identifier, NonnegativeAmount, PositiveAmount, UtcDateTime


class DisciplineLimits(DomainModel):
    policy_version: Identifier = "discipline-v1"
    max_buy_quantity: PositiveAmount | None = None
    max_sell_quantity: PositiveAmount | None = None
    max_position_quantity: PositiveAmount | None = None
    max_daily_loss_usd: NonnegativeAmount | None = None


class RiskContext(DomainModel):
    instrument: Instrument | None = None
    cost_buffer_rate: NonnegativeAmount | None = None
    cost_policy_version: Identifier | None = None
    daily_loss_usd: NonnegativeAmount | None = None
    daily_loss_as_of: UtcDateTime | None = None
    daily_loss_complete: StrictBool = False

    @model_validator(mode="after")
    def explicit_context(self) -> Self:
        if (self.cost_buffer_rate is None) != (self.cost_policy_version is None):
            raise ValueError("cost buffer requires an explicit version")
        if self.cost_buffer_rate is not None and self.cost_buffer_rate > 1:
            raise ValueError("cost buffer must be in 0..1")
        if (self.daily_loss_usd is None) != (self.daily_loss_as_of is None):
            raise ValueError("daily loss requires its sampling time")
        if self.daily_loss_complete and self.daily_loss_usd is None:
            raise ValueError("complete daily loss requires a value")
        return self


class RiskOutcome(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    UNAVAILABLE = "unavailable"


class RiskAssessment(DomainModel):
    snapshot_id: Identifier
    outcome: RiskOutcome
    reasons: tuple[Identifier, ...] = ()
    evaluated_at: UtcDateTime
    policy_version: Literal["risk-v1"] = "risk-v1"

    @model_validator(mode="after")
    def explicit_outcome(self) -> Self:
        if (self.outcome == RiskOutcome.ALLOW) != (not self.reasons):
            raise ValueError("risk outcome must agree with blocking reasons")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("risk reasons must be unique")
        return self

    @property
    def permits_advice(self) -> bool:
        return self.outcome == RiskOutcome.ALLOW
