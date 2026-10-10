"""Bounded local runtime state; hard alerts are independent of any model completion."""

from typing import Literal

from pydantic import Field, StrictBool

from .models import DomainModel, Identifier, UtcDateTime
from .sessions import SessionStatus


class HardAlertView(DomainModel):
    occurred_at: UtcDateTime
    reasons: tuple[Identifier, ...]


class DecisionRuntimeView(DomainModel):
    running: StrictBool
    inflight: StrictBool
    session_status: SessionStatus | None = None
    reason: Literal[
        "no_session",
        "session_not_running",
        "waiting",
        "current_evidence_unavailable",
        "deciding",
        "persistence",
        "invalid_data",
        "stopped",
    ]
    error: Literal["persistence", "invalid_data"] | None = None
    alerts: tuple[HardAlertView, ...] = Field(default=(), max_length=16)
