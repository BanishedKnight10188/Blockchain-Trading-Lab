"""Explicit human responses never mutate the agent's original assessment."""

from agent_platform.application.risk import RiskService
from agent_platform.domain.decisions import DecisionFeedback, FeedbackReceipt
from agent_platform.domain.feedback import FeedbackEvidence, FeedbackRecord
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.decisions import PublicationEvidencePort
from agent_platform.ports.feedback import FeedbackStorePort
from agent_platform.ports.persistence import RequestIdentityConflict, StateStorePort


class FeedbackService:
    def __init__(
        self,
        *,
        store: FeedbackStorePort,
        states: StateStorePort,
        snapshots: PublicationEvidencePort,
        clock: ClockPort,
        account_ref: str,
    ):
        self.store, self.states, self.snapshots, self.clock, self.account_ref = (
            store,
            states,
            snapshots,
            clock,
            account_ref,
        )
        self.risk = RiskService(clock)

    async def record(self, feedback: DecisionFeedback) -> FeedbackReceipt:
        record = FeedbackRecord.model_validate_json(
            FeedbackRecord(feedback=feedback, account_ref=self.account_ref).model_dump_json()
        )
        prior = await self.store.feedback(feedback.feedback_id, account_ref=self.account_ref)
        if prior is not None:
            if prior.feedback != record.feedback:
                raise RequestIdentityConflict("feedback identifier has different immutable inputs")
            return prior
        evidence = None
        if feedback.recommendation_id is not None:
            decision = await self.store.recommendation_decision(feedback.recommendation_id)
            state = await self.states.load(feedback.recommendation_id)
            if (
                decision is None
                or decision.completion is None
                or state is None
                or state.state_type != "recommendation"
                or state.state.status != "published"
            ):
                raise ValueError("feedback requires current published advice")
            current, risk = None, None
            if feedback.kind == "accepted":
                current = await self.snapshots.capture(decision.request)
                if current is None:
                    raise ValueError("current feedback evidence is unavailable")
                risk = self.risk.evaluate(
                    current, state.state.assessment, context=decision.completion.risk_context
                )
            evidence = FeedbackEvidence(
                request=decision.request,
                completion=decision.completion,
                current=current,
                expected_revision=state.revision,
                risk=risk,
            )
        return await self.store.record_feedback(record, evidence)
