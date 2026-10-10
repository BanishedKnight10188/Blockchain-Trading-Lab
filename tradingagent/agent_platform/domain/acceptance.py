"""Offline smoke evidence is explicitly distinct from formal first-release acceptance."""

from typing import Literal

from pydantic import Field, StrictBool

from .models import DomainModel, UtcDateTime


class AcceptanceCheck(DomainModel):
    name: Literal[
        "default_network_disabled",
        "paid_models_disabled",
        "jev_unspecified",
        "safe_runtime_running",
        "budget_readable",
        "funds_routes_absent",
        "owned_workers_stopped",
    ]
    passed: StrictBool


class AcceptanceReport(DomainModel):
    evidence_kind: Literal["AUTOMATED_OFFLINE"] = "AUTOMATED_OFFLINE"
    scope: Literal["local_assembly_smoke"] = "local_assembly_smoke"
    generated_at: UtcDateTime
    outcome: Literal["passed_with_gaps", "failed"]
    checks: tuple[AcceptanceCheck, ...] = Field(max_length=32)
    formal_acceptance: Literal[False] = False
    shutdown_status: Literal["stopped", "not_started", "degraded"]
    failure: Literal["local_assembly_unavailable"] | None = None
    gaps: tuple[
        Literal[
            "manual_read_only_pending",
            "soak_pending",
            "model_provider_pending",
            "mcp_mapping_pending",
            "jev_unspecified",
            "discipline_configuration_pending",
        ],
        ...,
    ] = Field(max_length=6)
