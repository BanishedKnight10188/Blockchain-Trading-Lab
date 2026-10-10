"""Bounded process health containing only safe diagnostic codes."""

from typing import Literal

from pydantic import Field, StrictBool

from .models import DomainModel

WorkerFailure = Literal[
    "credentials",
    "authentication",
    "rate_limit",
    "transport",
    "invalid_data",
    "persistence",
    "review_jobs_unavailable",
    "reconcile_unavailable",
    "worker_unavailable",
    "paper_unavailable",
    "futures_unavailable",
]


class WorkerHealth(DomainModel):
    name: Literal[
        "data",
        "decisions",
        "reviews",
        "private_stream",
        "archive",
        "diagnostics",
        "worker",
        "paper",
        "futures",
    ]
    running: StrictBool
    failure: WorkerFailure | None = None


class RuntimeHealth(DomainModel):
    status: Literal["starting", "running", "degraded", "paused", "stopping", "stopped"]
    worker_count: int = Field(strict=True, ge=0, le=9)
    workers: tuple[WorkerHealth, ...] = Field(max_length=9)
    failure: Literal["worker_start_failed", "worker_stop_failed"] | None = None
