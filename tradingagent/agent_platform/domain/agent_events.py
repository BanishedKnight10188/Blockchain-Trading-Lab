"""Immutable trigger facts and separately mutable delivery ownership."""

from hashlib import sha256
from typing import Literal

from pydantic import Field

from .models import DomainModel, Identifier, UtcDateTime
from .watches import WatchDefinition, WatchEvaluation, WatchFrame


def watch_event_id(definition: WatchDefinition, frame: WatchFrame) -> str:
    key = f"{definition.watch_id}:{definition.version}:{frame.candle_key}"
    return "watch-event:" + sha256(key.encode()).hexdigest()


class WatchEvent(DomainModel):
    event_id: Identifier
    lane_id: Identifier
    session_id: Identifier
    watch_id: Identifier
    definition_revision: int = Field(strict=True, ge=1)
    occurred_at: UtcDateTime
    expires_at: UtcDateTime
    rule_hash: Identifier
    frame: WatchFrame
    evaluation: WatchEvaluation


class EventDelivery(DomainModel):
    event_id: Identifier
    status: Literal[
        "QUEUED", "LEASED", "DISPATCHED", "RECONCILING", "COMPLETED", "FAILED", "SUPPRESSED"
    ] = "QUEUED"
    attempts: int = Field(default=0, strict=True, ge=0, le=3)
    lease_token: Identifier | None = None
    lease_until: UtcDateTime | None = None
    run_id: Identifier | None = None
    reason: str | None = None


class EventLease(DomainModel):
    event: WatchEvent
    lease_token: Identifier
    lease_until: UtcDateTime
