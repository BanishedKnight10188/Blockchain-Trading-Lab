"""Structured decision inference is separate from prose generation."""

from typing import Protocol, runtime_checkable

from agent_platform.domain.decision_models import DecisionModelRequest, DecisionModelResponse
from agent_platform.domain.routing import ModelCostQuote


@runtime_checkable
class DecisionModelPort(Protocol):
    async def decide(
        self, request: DecisionModelRequest, quote: ModelCostQuote
    ) -> DecisionModelResponse: ...
