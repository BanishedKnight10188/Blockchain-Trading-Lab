"""Free deterministic review facts and explicit retrospective versions."""

from datetime import datetime

from agent_platform.domain.common import live_account_ref, required_identifier, utc_datetime
from agent_platform.domain.review_records import (
    FrozenReviewGroup,
    ReviewProposal,
    rule_review_explanation,
)
from agent_platform.domain.reviews import ReviewKind, ReviewRevision, TradeGroup
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.persistence import RequestIdentityConflict
from agent_platform.ports.reviews import ReviewStorePort

from .trade_groups import FifoAnalyzer


class ReviewService:
    def __init__(self, *, store: ReviewStorePort, clock: ClockPort, account_ref: str):
        self.store, self.clock, self.account_ref = store, clock, live_account_ref(account_ref)

    async def create_group(self, group_id: str, cutoff: datetime) -> FrozenReviewGroup:
        identity, cutoff = required_identifier(group_id), utc_datetime(cutoff)
        if len(identity) > 128 or cutoff > self.clock.utcnow():
            raise ValueError("group identity and cutoff must be bounded")
        prior = await self.store.review_group(identity, account_ref=self.account_ref)
        if prior:
            if prior.facts.data_cutoff != cutoff:
                raise RequestIdentityConflict("group identity already has another cutoff")
            return prior
        batch = await self.store.review_history(self.account_ref, cutoff)
        if not batch.trades:
            raise ValueError("review requires actual imported exchange trades")
        facts = FifoAnalyzer().analyze(batch, cutoff)
        group = TradeGroup(
            trade_group_id=identity,
            account_ref=self.account_ref,
            symbol="BTCUSDT",
            trades=batch.trades,
            cost_status=facts.cost_status,
        )
        return await self.store.freeze_review_group(
            FrozenReviewGroup(
                group=group, batch=batch, facts=facts, registered_at=self.clock.utcnow()
            )
        )

    async def generate(self, group_id: str, cutoff: datetime, kind: ReviewKind) -> ReviewRevision:
        identity, cutoff, kind = (
            required_identifier(group_id),
            utc_datetime(cutoff),
            ReviewKind(kind),
        )
        group = await self.store.review_group(identity, account_ref=self.account_ref)
        if group is None or not group.facts.data_cutoff <= cutoff <= self.clock.utcnow():
            raise ValueError("review requires frozen facts before the current cutoff")
        explanation = rule_review_explanation(group.facts.cost_status, kind)
        record = await self.store.record_review(
            ReviewProposal(
                trade_group_id=identity,
                account_ref=self.account_ref,
                kind=kind,
                data_cutoff=cutoff,
                generated_at=self.clock.utcnow(),
                explanation=explanation,
            )
        )
        return record.review
