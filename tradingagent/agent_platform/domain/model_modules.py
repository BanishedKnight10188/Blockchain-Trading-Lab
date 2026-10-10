"""Model selection is owned configuration, never permission to invoke a model."""

import re
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StrictBool, model_validator

from .models import DomainModel


def _model_id(value):
    if (
        type(value) is not str
        or len(value) > 128
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,96}", value)
        is None
    ):
        raise ValueError("model requires a bounded provider/model identity")
    return value


ModelId = Annotated[str, BeforeValidator(_model_id)]


class StrongModelSelection(DomainModel):
    model_id: ModelId | None = None
    calls_enabled: StrictBool = False

    @model_validator(mode="after")
    def disabled_in_this_release(self) -> Self:
        if self.calls_enabled:
            raise ValueError("strong model calls are disabled in this release")
        return self


class JevModuleSettings(DomainModel):
    """Optional advice lane; this switch never controls the trading lane."""

    enabled: StrictBool = False
    mode: Literal["independent_position"] = "independent_position"
    account_provider: Literal["binance_agent_os"] = "binance_agent_os"


class JevTraderSettings(DomainModel):
    enabled: StrictBool = False
    account_provider: Literal["binance_agent_os"] = "binance_agent_os"


class ModelModulesConfig(DomainModel):
    analysis_mode: Literal["continuous"] = "continuous"
    analysis_model: Literal["anthropic/claude-haiku-5.5", "deepseek/deepseek-v4.1-flash"] = (
        "anthropic/claude-haiku-5.5"
    )
    decision_model: Literal["typesafe/jev-1.13"] = "typesafe/jev-1.13"
    strong_model: StrongModelSelection = Field(default_factory=StrongModelSelection)
    jev: JevModuleSettings = Field(default_factory=JevModuleSettings)
    jev_trader: JevTraderSettings = Field(default_factory=JevTraderSettings)

    @model_validator(mode="after")
    def distinct_roles(self) -> Self:
        if self.strong_model.model_id in (
            "anthropic/claude-haiku-5.5",
            "deepseek/deepseek-v4.1-flash",
            self.decision_model,
        ):
            raise ValueError("strong selection cannot overlap executable models")
        return self

    def public_state(self) -> dict:
        return {
            "analysis_model": self.analysis_model,
            "analysis_mode": self.analysis_mode,
            "decision_model": self.decision_model,
            "strong_model": {"model_id": self.strong_model.model_id, "calls_enabled": False},
            "jev": self.jev.model_dump(mode="json"),
            "jev_trader": self.jev_trader.model_dump(mode="json"),
        }
