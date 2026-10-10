"""Only fixed diagnostic events may enter the operational log."""

from typing import Literal

from .health import RuntimeHealth
from .models import DomainModel, UtcDateTime


class OperationalEvent(DomainModel):
    event: Literal["runtime_started", "worker_health", "runtime_stopped"]
    occurred_at: UtcDateTime
    health: RuntimeHealth
