"""Finite response facts, safe for persistence without provider content."""

from typing import Literal

from pydantic import Field, model_serializer

from .models import DomainModel, FiniteDecimal


class ModelTransportEvidence(DomainModel):
    """Local monotonic durations; response wait also includes remote/network work."""

    attempts: int = Field(strict=True, ge=1, le=2)
    pre_request_retries: int = Field(strict=True, ge=0, le=1)
    request_started: bool = Field(strict=True)
    connect_ms: int = Field(strict=True, ge=0)
    proxy_connect_ms: int = Field(strict=True, ge=0)
    tls_ms: int = Field(strict=True, ge=0)
    send_ms: int = Field(strict=True, ge=0)
    response_wait_ms: int = Field(strict=True, ge=0)
    body_ms: int = Field(strict=True, ge=0)
    total_ms: int = Field(strict=True, ge=0)
    failed_phase: (
        Literal["connect", "proxy_connect", "tls", "send", "response_wait", "body", "unknown"]
        | None
    ) = None
    error_type: (
        Literal[
            "ConnectError",
            "ConnectTimeout",
            "ReadError",
            "ReadTimeout",
            "WriteError",
            "WriteTimeout",
            "PoolTimeout",
            "ProxyError",
            "RemoteProtocolError",
            "LocalProtocolError",
            "DeadlineTimeout",
            "HTTPError",
        ]
        | None
    ) = None


class ModelLocalTiming(DomainModel):
    """Disjoint application phases in microseconds, including local IO/queue waits."""

    validation_us: int = Field(strict=True, ge=0)
    reserve_us: int = Field(strict=True, ge=0)
    provider_us: int = Field(strict=True, ge=0)
    fee_validation_us: int = Field(strict=True, ge=0)
    settle_us: int = Field(strict=True, ge=0)
    binding_us: int = Field(strict=True, ge=0)
    total_us: int = Field(strict=True, ge=0)


class ModelDiagnostic(DomainModel):
    stage: Literal[
        "transport",
        "usage",
        "metadata",
        "question_set",
        "answer_type",
        "criteria",
        "legend",
        "answer_values",
        "binding",
    ]
    question_index: int | None = Field(default=None, strict=True, ge=0, le=7)
    expected_count: int | None = Field(default=None, strict=True, ge=0, le=255)
    actual_count: int | None = Field(default=None, strict=True, ge=0, le=256)
    issue: Literal["probability_sum", "invalid_answer_values"] | None = None
    probability_total: FiniteDecimal | None = Field(default=None, ge=0, le=255)
    transport_evidence: ModelTransportEvidence | None = None
    local_timing: ModelLocalTiming | None = None

    @model_serializer(mode="wrap")
    def preserve_old_diagnostic(self, handler):
        value = handler(self)
        if self.transport_evidence is None:
            value.pop("transport_evidence", None)
        if self.local_timing is None:
            value.pop("local_timing", None)
        return value
