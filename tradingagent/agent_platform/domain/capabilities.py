"""Schema observations are evidence for review, never execution authorization."""

from typing import Annotated, Literal

from pydantic import Field

from .models import DomainModel, UtcDateTime

Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ToolName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:/-]{1,128}$")]


class ToolFingerprint(DomainModel):
    name: ToolName
    input_sha256: Fingerprint
    output_sha256: Fingerprint | None = None
    metadata_sha256: Fingerprint


class CapabilityReport(DomainModel):
    provider: Literal["binance_agent_os"] = "binance_agent_os"
    checked_at: UtcDateTime
    status: Literal["disabled", "unavailable", "unverified", "schema_matched", "schema_changed"]
    tools: tuple[ToolFingerprint, ...] = ()
    # No verified tool mapping or authorization has been recorded for this project.
    verified_capabilities: tuple[()] = ()
    reason: (
        Literal["permission", "transport", "timeout", "invalid_metadata", "incomplete_inventory"]
        | None
    ) = None
