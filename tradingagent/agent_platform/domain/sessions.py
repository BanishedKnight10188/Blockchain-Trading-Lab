"""Continuous user-selected trading style and immutable session revisions."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import model_validator

from .common import utc_datetime
from .models import (
    DomainModel,
    Identifier,
    NonnegativeAmount,
    Revision,
    StyleStrength,
    UtcDateTime,
)
from .session_market import SessionAnalysisTarget


class StyleContext(DomainModel):
    strength: StyleStrength
    policy_version: Literal["style-v1"] = "style-v1"
    aggression_weight: NonnegativeAmount
    conservatism_weight: NonnegativeAmount

    @model_validator(mode="after")
    def exact_weights(self) -> Self:
        if self.aggression_weight != Decimal(f"{self.strength}e-2"):
            raise ValueError("style weight does not match strength")
        if self.conservatism_weight != Decimal(f"{100 - self.strength}e-2"):
            raise ValueError("conservatism weight does not match strength")
        return self


class TradingStyle(DomainModel):
    strength: StyleStrength
    policy_version: Literal["style-v1"] = "style-v1"

    @property
    def label(self) -> str:
        if self.strength <= 33:
            return "偏保守"
        return "均衡" if self.strength <= 66 else "偏激进"

    @property
    def context(self) -> StyleContext:
        return StyleContext(
            strength=self.strength,
            aggression_weight=Decimal(f"{self.strength}e-2"),
            conservatism_weight=Decimal(f"{100 - self.strength}e-2"),
        )


class SessionStatus(StrEnum):
    CONFIGURED = "configured"
    RUNNING = "running"
    PAUSED = "paused"
    CLOSED = "closed"


_SESSION_TRANSITIONS = {
    SessionStatus.CONFIGURED: frozenset((SessionStatus.RUNNING, SessionStatus.CLOSED)),
    SessionStatus.RUNNING: frozenset((SessionStatus.PAUSED, SessionStatus.CLOSED)),
    SessionStatus.PAUSED: frozenset((SessionStatus.RUNNING, SessionStatus.CLOSED)),
}


class AgentSession(DomainModel):
    session_id: Identifier
    style: TradingStyle
    analysis_target: SessionAnalysisTarget = SessionAnalysisTarget()
    revision: Revision = 1
    style_revision: Revision = 1
    status: SessionStatus = SessionStatus.CONFIGURED
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def consistent_revision(self) -> Self:
        if self.style_revision > self.revision or self.updated_at < self.created_at:
            raise ValueError("session revision or chronology is inconsistent")
        return self

    def change_style(self, style: TradingStyle, at: datetime) -> Self:
        if self.status == SessionStatus.CLOSED:
            raise ValueError("closed session cannot change style")
        timestamp = utc_datetime(at)
        if timestamp < self.updated_at:
            raise ValueError("session updates cannot move back in time")
        if self.style == style:
            return self
        return type(self).model_validate(
            {
                **self.model_dump(),
                "style": style,
                "updated_at": timestamp,
                "revision": self.revision + 1,
                "style_revision": self.style_revision + 1,
            }
        )

    def matches_style_revision(self, revision: int) -> bool:
        return type(revision) is int and revision == self.style_revision

    def transition(self, status: SessionStatus | str, at: datetime) -> Self:
        target = SessionStatus(status)
        timestamp = utc_datetime(at)
        if timestamp < self.updated_at:
            raise ValueError("session updates cannot move back in time")
        if target == self.status:
            return self
        if target not in _SESSION_TRANSITIONS.get(self.status, ()):
            raise ValueError("session state transition is not allowed")
        return type(self).model_validate(
            {
                **self.model_dump(),
                "status": target,
                "updated_at": timestamp,
                "revision": self.revision + 1,
            }
        )
