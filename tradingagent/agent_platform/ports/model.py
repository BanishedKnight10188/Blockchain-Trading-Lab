"""Model generation uses finite owned requests and responses."""

import re
from typing import Protocol, runtime_checkable

from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.model_calls import ModelRequest, ModelResponse
from agent_platform.domain.model_diagnostics import ModelDiagnostic


class ModelCallFailed(RuntimeError):
    """Sanitized failure which can retain independently validated fee evidence."""

    def __init__(
        self,
        reason: str,
        usage: ModelUsage | None = None,
        diagnostic: ModelDiagnostic | None = None,
    ):
        self.reason = (
            reason
            if reason
            in {
                "invalid_model_assessment",
                "invalid_model_usage",
                "invalid_model_metadata",
                "model_module_disabled",
                "model_read_only",
                "provider_error",
                "provider_access_denied",
                "provider_transport_error",
                "provider_timeout",
                "invalid_model_response",
                "quote_unavailable",
                "decision_expired",
                "verification_complete",
            }
            or (type(reason) is str and re.fullmatch(r"provider_http_[1-5][0-9]{2}", reason))
            else "provider_error"
        )
        self.usage = usage
        self.diagnostic = (
            ModelDiagnostic.model_validate_json(diagnostic.model_dump_json())
            if isinstance(diagnostic, ModelDiagnostic)
            else None
        )
        super().__init__(self.reason)


@runtime_checkable
class ModelPort(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
