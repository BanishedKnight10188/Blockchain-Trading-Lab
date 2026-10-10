"""Feedback is a local fact; this port cannot execute a trade."""

from typing import Protocol

from agent_platform.domain.decision_requests import DecisionReadRecord
from agent_platform.domain.decisions import FeedbackReceipt
from agent_platform.domain.feedback import FeedbackEvidence, FeedbackRecord


class FeedbackStorePort(Protocol):
    async def feedback(self, feedback_id: str, *, account_ref: str) -> FeedbackReceipt | None: ...

    async def recommendation_decision(
        self, recommendation_id: str
    ) -> DecisionReadRecord | None: ...

    async def record_feedback(
        self, record: FeedbackRecord, evidence: FeedbackEvidence | None
    ) -> FeedbackReceipt: ...
