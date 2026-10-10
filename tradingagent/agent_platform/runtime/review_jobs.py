"""Explicit persistent review jobs, bounded polling and a graceful local worker."""

import asyncio
from datetime import datetime, timedelta

from agent_platform.domain.common import live_account_ref, required_identifier, utc_datetime
from agent_platform.domain.review_jobs import review_job_identity
from agent_platform.domain.reviews import ReviewJob, ReviewKind
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.review_jobs import ReviewJobStorePort


class ReviewJobService:
    def __init__(self, *, store: ReviewJobStorePort, clock: ClockPort, account_ref: str):
        self.store, self.clock, self.account_ref = store, clock, live_account_ref(account_ref)
        self._run_lock = asyncio.Lock()
        self._task = None
        self._stopping = asyncio.Event()
        self.last_failure = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def schedule(self, group_id: str, due_at: datetime, kind: ReviewKind) -> ReviewJob:
        identity, due, kind = required_identifier(group_id), utc_datetime(due_at), ReviewKind(kind)
        now = self.clock.utcnow()
        if len(identity) > 128:
            raise ValueError("job requires a bounded group identity")
        job_id = review_job_identity(identity, kind, due)
        prior = await self.store.review_schedule(job_id, account_ref=self.account_ref)
        if prior is not None:
            return prior
        if due < now:
            raise ValueError("job requires a bounded group and an explicit future due time")
        return await self.store.schedule_review_job(
            ReviewJob(
                job_id=job_id,
                trade_group_id=identity,
                kind=kind,
                created_at=now,
                scheduled_for=due,
            ),
            account_ref=self.account_ref,
        )

    async def schedule_followups(
        self, group_id: str, hours: tuple[int, ...]
    ) -> tuple[ReviewJob, ...]:
        if (
            len(hours) > 2
            or any(type(hour) is not int or hour not in (1, 24) for hour in hours)
            or len(set(hours)) != len(hours)
        ):
            raise ValueError("explicit followups support unique 1h and 24h choices")
        group = await self.store.review_group(group_id, account_ref=self.account_ref)
        if group is None:
            raise ValueError("followup requires a local frozen group")
        return tuple(
            [
                await self.schedule(
                    group_id,
                    group.group.trades[-1].executed_at + timedelta(hours=hour),
                    ReviewKind.FOLLOWUP,
                )
                for hour in hours
            ]
        )

    async def cancel(self, job_id: str, expected_revision: int) -> ReviewJob:
        return await self.store.cancel_review_job(
            job_id, expected_revision, account_ref=self.account_ref
        )

    async def run_due(self, *, limit=10) -> tuple[ReviewJob, ...]:
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("one job poll supports 1–10 due tasks")
        async with self._run_lock:
            jobs = await self.store.review_jobs(
                self.account_ref, due_before=self.clock.utcnow(), limit=limit
            )
            return tuple(
                [
                    await self.store.run_review_job(job.job_id, account_ref=self.account_ref)
                    for job in jobs
                ]
            )

    async def start(self):
        if not self.running:
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while not self._stopping.is_set():
            try:
                await self.run_due()
                self.last_failure = None
            except Exception:
                self.last_failure = "review_jobs_unavailable"
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1)
            except TimeoutError:
                pass

    async def stop(self):
        self._stopping.set()
        task = self._task
        if task is not None:
            await task
            if self._task is task:
                self._task = None
