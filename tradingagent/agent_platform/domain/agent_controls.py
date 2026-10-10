"""Versioned local operating intent; no credentials or executable order parameters."""

from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .model_modules import JevModuleSettings, JevTraderSettings
from .models import DomainModel, UtcDateTime
from .operating_modes import OperatingSettings


class ConfirmedSelection(DomainModel):
    confirmed: StrictBool

    @model_validator(mode="after")
    def explicitly_confirmed(self) -> Self:
        if not self.confirmed:
            raise ValueError("operating selection requires explicit confirmation")
        return self


class ControlSelection(ConfirmedSelection):
    mode: Literal["advisory", "auto"]
    execution_environment: Literal["paper", "testnet"] = "testnet"
    jev_enabled: StrictBool
    trader_enabled: StrictBool


class AdviceSelection(ConfirmedSelection):
    enabled: StrictBool


class TraderSelection(ConfirmedSelection):
    mode: Literal["advisory", "auto"]
    execution_environment: Literal["paper", "testnet"] = "testnet"
    enabled: StrictBool


class AgentControlState(DomainModel):
    aggregate_id: Literal["agent-controls"] = "agent-controls"
    revision: int = Field(strict=True, ge=0)
    updated_at: UtcDateTime
    operation: OperatingSettings
    jev: JevModuleSettings
    trader: JevTraderSettings = Field(default_factory=JevTraderSettings)
    advice_revision: int = Field(default=1, strict=True, ge=1)
    trader_revision: int = Field(default=1, strict=True, ge=1)
