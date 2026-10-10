"""Explicit immutable workbench intent, independent of the displayed task."""

from typing import Literal

from pydantic import Field, StrictBool, model_serializer, model_validator

from .agent_controls import ConfirmedSelection
from .models import DomainModel, Identifier, StyleStrength
from .session_market import SessionAnalysisTarget
from .trading_runtime import TradingLimits, TradingPolicy


class JevTaskCreate(ConfirmedSelection):
    request_id: Identifier
    kind: Literal["jev_advice", "jev_trader"]
    name: str = Field(default="", strict=True, max_length=80)
    analysis_target: SessionAnalysisTarget
    style_strength: StyleStrength
    style_confirmed: StrictBool
    limits: TradingLimits
    policy: TradingPolicy
    start: StrictBool = True
    decision_interval_seconds: int = Field(default=1, strict=True, ge=1, le=10)
    context_mode: Literal["legacy", "multiscale"] = "legacy"

    @model_serializer(mode="wrap")
    def compatible_selection(self, handler):
        result = handler(self)
        if self.decision_interval_seconds == 1:
            result.pop("decision_interval_seconds", None)
        if self.context_mode == "legacy":
            result.pop("context_mode", None)
        return result

    @model_validator(mode="after")
    def confirmed_futures(self):
        if not self.style_confirmed or self.analysis_target.market != "usdt_perpetual":
            raise ValueError("explicit futures style selection is required")
        return self


class JevTaskRecord(DomainModel):
    task_id: Identifier
    request_id: Identifier
    kind: Literal["jev_advice", "jev_trader"]
    name: str
    session_id: Identifier | None = None
    legacy: bool = False
    selection: JevTaskCreate | None = None
    setup: Literal["creating", "ready"] = "creating"
    failure: str | None = None
    decision_interval_seconds: int | None = Field(default=None, strict=True, ge=1, le=10)
    cadence_revision: int = Field(default=0, strict=True, ge=0)
    context_mode: Literal["legacy", "multiscale"] | None = None
    context_revision: int = Field(default=0, strict=True, ge=0)

    @model_serializer(mode="wrap")
    def compatible_record(self, handler):
        result = handler(self)
        if self.decision_interval_seconds is None:
            result.pop("decision_interval_seconds", None)
        if self.cadence_revision == 0:
            result.pop("cadence_revision", None)
        if self.context_mode is None:
            result.pop("context_mode", None)
        if self.context_revision == 0:
            result.pop("context_revision", None)
        return result
