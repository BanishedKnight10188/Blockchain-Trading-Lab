"""Synchronous deterministic advice; JEV requires a separate defined implementation."""

from typing import Protocol, runtime_checkable

from agent_platform.domain.decisions import AdvisoryAssessment, DecisionSnapshot


@runtime_checkable
class AdvisoryPort(Protocol):
    def evaluate(self, snapshot: DecisionSnapshot) -> AdvisoryAssessment: ...
