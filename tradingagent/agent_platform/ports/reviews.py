"""Atomic frozen groups and append-only review version storage."""

from datetime import datetime
from typing import Protocol

from agent_platform.domain.account import TradeBatch
from agent_platform.domain.review_records import (
    FrozenReviewGroup,
    ReviewGroupPage,
    ReviewProposal,
    ReviewRecord,
)


class ReviewStorePort(Protocol):
    async def review_groups(
        self, account_ref: str, *, after_sequence: int = 0, limit: int = 50
    ) -> ReviewGroupPage: ...
    async def review_history(self, account_ref: str, cutoff: datetime) -> TradeBatch: ...
    async def review_group(
        self, group_id: str, *, account_ref: str
    ) -> FrozenReviewGroup | None: ...
    async def freeze_review_group(self, group: FrozenReviewGroup) -> FrozenReviewGroup: ...
    async def record_review(self, proposal: ReviewProposal) -> ReviewRecord: ...
    async def review_versions(
        self, group_id: str, *, account_ref: str, after_revision: int = 0, limit: int = 50
    ) -> tuple[ReviewRecord, ...]: ...
