"""Claim once and publish with authoritative session/account checks in one commit."""

from typing import Protocol, runtime_checkable

from agent_platform.domain.decision_requests import (
    DecisionClaimReceipt,
    DecisionCompletion,
    DecisionReadRecord,
    DecisionRequest,
)
from agent_platform.domain.decisions import DecisionResult, DecisionSnapshot


@runtime_checkable
class DecisionStorePort(Protocol):
    async def claim(self, request: DecisionRequest) -> DecisionClaimReceipt: ...

    async def decision(self, request_id: str) -> DecisionClaimReceipt | None: ...

    async def finish(self, request_id: str, completion: DecisionCompletion) -> DecisionResult: ...


class PublicationEvidencePort(Protocol):
    async def capture(self, request: DecisionRequest) -> DecisionSnapshot | None: ...


class DecisionReadPort(Protocol):
    async def latest_decision(self, session_id: str) -> DecisionReadRecord | None: ...
