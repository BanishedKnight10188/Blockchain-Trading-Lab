"""Explicit durable task scheduling and atomic free rule-review completion."""

from datetime import datetime

from agent_platform.domain.reviews import ReviewJob

from .reviews import ReviewStorePort


class ReviewJobStorePort(ReviewStorePort):
    async def review_schedule(self, job_id: str, *, account_ref: str) -> ReviewJob | None: ...
    async def schedule_review_job(self, job: ReviewJob, *, account_ref: str) -> ReviewJob: ...
    async def review_jobs(
        self, account_ref: str, *, due_before: datetime | None = None, limit: int = 50
    ) -> tuple[ReviewJob, ...]: ...
    async def run_review_job(self, job_id: str, *, account_ref: str) -> ReviewJob: ...
    async def cancel_review_job(
        self, job_id: str, expected_revision: int, *, account_ref: str
    ) -> ReviewJob: ...
