"""Persistent explicit followups complete one rule review and task atomically."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_reviews import service as review_service

context = _context


async def service(context):
    reviews, store, clock = await review_service(context)
    adapter = importlib.import_module("agent_platform.adapters.sqlite.review_jobs")
    store = adapter.SqliteReviewJobStore(context[0], clock=clock)
    group = await reviews.create_group("group-1", clock.utcnow())
    await reviews.generate("group-1", clock.utcnow(), "initial")
    application = importlib.import_module("agent_platform.runtime.review_jobs")
    return (
        application.ReviewJobService(store=store, clock=clock, account_ref="local-spot"),
        store,
        clock,
        group,
    )


@pytest.mark.asyncio
async def test_schedule_identity_is_group_kind_and_due_not_random_request(context):
    value, store, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(hours=1)
    one = await value.schedule("group-1", due, "followup")
    two = await value.schedule("group-1", due, "followup")
    assert one == two and one.status == "pending" and one.review_id is None
    assert len(await store.review_jobs("local-spot")) == 1


@pytest.mark.asyncio
async def test_followup_choices_are_explicit_one_or_twenty_four_hours(context):
    value, store, _, group = await service(context)
    assert await value.schedule_followups("group-1", ()) == ()
    jobs = await value.schedule_followups("group-1", (1, 24))
    assert [job.scheduled_for for job in jobs] == [
        group.group.trades[-1].executed_at + timedelta(hours=1),
        group.group.trades[-1].executed_at + timedelta(hours=24),
    ]
    assert len(await store.review_jobs("local-spot")) == 2
    with pytest.raises(ValueError):
        await value.schedule_followups("group-1", (12,))


@pytest.mark.asyncio
async def test_due_job_freezes_scheduled_cutoff_and_generates_once(context):
    value, store, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(hours=1)
    job = await value.schedule("group-1", due, "followup")
    assert await value.run_due() == ()
    clock.advance_to(due + timedelta(minutes=10))
    result = await value.run_due()
    assert len(result) == 1 and result[0].status == "done" and result[0].job_id == job.job_id
    records = await store.review_versions("group-1", account_ref="local-spot")
    assert len(records) == 2 and records[-1].review.data_cutoff == due
    assert (
        records[-1].review.generated_at == clock.utcnow()
        and not records[-1].review.model_participated
    )
    assert await value.run_due() == ()


@pytest.mark.asyncio
async def test_restart_and_two_instances_do_not_duplicate_review(context):
    value, store, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(seconds=1)
    await value.schedule("group-1", due, "followup")
    reopened = type(value)(
        store=type(store)(context[0], clock=clock), clock=clock, account_ref="local-spot"
    )
    clock.advance_to(due)
    await asyncio.gather(value.run_due(), reopened.run_due())
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 2
    assert (await store.review_jobs("local-spot"))[0].status == "done"


@pytest.mark.asyncio
async def test_completion_audit_failure_rolls_back_review_and_leaves_job_pending(
    context, monkeypatch
):
    value, store, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(seconds=1)
    await value.schedule("group-1", due, "followup")
    clock.advance_to(due)
    append = store._append

    def failing(connection, event):
        if event.kind == "review_job_recorded" and event.payload.status == "done":
            raise OSError("offline completion failure")
        return append(connection, event)

    monkeypatch.setattr(store, "_append", failing)
    with pytest.raises(PersistenceUnavailable):
        await value.run_due()
    assert (await store.review_jobs("local-spot"))[0].status == "pending"
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 1
    monkeypatch.setattr(store, "_append", append)
    assert (await value.run_due())[0].status == "done"
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 2


@pytest.mark.asyncio
async def test_cancel_uses_revision_and_never_creates_review(context):
    value, store, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(seconds=1)
    job = await value.schedule("group-1", due, "followup")
    with pytest.raises(RevisionConflict):
        await value.cancel(job.job_id, 2)
    canceled = await value.cancel(job.job_id, 1)
    assert canceled.status == "canceled"
    clock.advance_to(due)
    assert await value.run_due() == ()
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group,offset,scope",
    [("absent", 1, "local-spot"), ("group-1", -1, "local-spot"), ("group-1", 1, "foreign")],
)
async def test_invalid_schedule_fails_without_task(context, group, offset, scope):
    value, store, clock, _ = await service(context)
    if scope != "local-spot":
        value = type(value)(store=store, clock=clock, account_ref=scope)
    with pytest.raises(ValueError):
        await value.schedule(group, clock.utcnow() + timedelta(seconds=offset), "followup")
    assert await store.review_jobs("local-spot") == ()


@pytest.mark.asyncio
async def test_lifecycle_invalid_job_fails_explicitly_and_does_not_retry_forever(context):
    value, store, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(seconds=1)
    await value.schedule("group-1", due, "initial")
    clock.advance_to(due)
    result = await value.run_due()
    assert (
        result[0].status == "failed" and result[0].failure_reason == "review_conditions_unsatisfied"
    )
    assert await value.run_due() == ()
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 1


@pytest.mark.asyncio
async def test_background_worker_starts_and_stops_without_pending_poll(context):
    value, _, _, _ = await service(context)
    await value.start()
    assert value.running
    await value.stop()
    assert not value.running


@pytest.mark.asyncio
async def test_due_microsecond_order_is_applied_before_limited_selection(context):
    value, store, clock, _ = await service(context)
    early = clock.utcnow().replace(microsecond=0) + timedelta(seconds=1)
    late = early + timedelta(microseconds=1)
    first = await value.schedule("group-1", early, "followup")
    second = await value.schedule("group-1", late, "followup")
    clock.advance_to(late)
    assert (await value.run_due(limit=1))[0].job_id == first.job_id
    assert (await value.run_due(limit=1))[0].job_id == second.job_id
    assert all(job.status == "done" for job in await store.review_jobs("local-spot"))


@pytest.mark.asyncio
async def test_schedule_retry_after_due_returns_original_complete_receipt(context):
    value, _, clock, _ = await service(context)
    due = clock.utcnow() + timedelta(seconds=1)
    scheduled = await value.schedule("group-1", due, "followup")
    clock.advance_to(due + timedelta(seconds=1))
    assert await value.schedule("group-1", due, "followup") == scheduled
    await value.run_due()
    assert await value.schedule("group-1", due, "followup") == scheduled


@pytest.mark.asyncio
async def test_complete_terminal_phases_cannot_precede_due_or_real_review(context):
    from agent_platform.application.reviews import ReviewService
    from agent_platform.domain.events import JournalEvent, StateRecord
    from agent_platform.domain.reviews import ReviewJobStatus
    from agent_platform.ports.persistence import EventIdentityConflict

    value, store, clock, _ = await service(context)
    created = clock.utcnow()
    due = created + timedelta(seconds=1)
    job = await value.schedule("group-1", due, "followup")
    clock.advance_to(due + timedelta(seconds=1))
    review = await ReviewService(store=store, clock=clock, account_ref="local-spot").generate(
        "group-1", due, "followup"
    )
    done = job.model_copy(update={"status": ReviewJobStatus.DONE, "review_id": review.review_id})
    state = StateRecord(
        key=job.job_id, revision=2, state_type="review_job", state=done, updated_at=created
    )
    await store.save(
        state,
        1,
        JournalEvent(
            event_id=job.job_id + ":terminal",
            aggregate_id=job.job_id,
            kind="state_changed",
            payload=state,
            occurred_at=created,
        ),
    )
    await store.append(
        JournalEvent(
            event_id=job.job_id + ":terminal:fact",
            aggregate_id=job.job_id,
            kind="review_job_recorded",
            payload=done,
            occurred_at=created,
        )
    )
    with pytest.raises(EventIdentityConflict):
        await store.review_jobs("local-spot")


@pytest.mark.asyncio
async def test_completion_clock_rewind_rolls_back_new_review_and_job(context, monkeypatch):
    value, store, clock, _ = await service(context)
    created = clock.utcnow()
    due = created + timedelta(seconds=1)
    await value.schedule("group-1", due, "followup")
    clock.advance_to(due + timedelta(seconds=1))
    reads = 0

    def rewinding_time():
        nonlocal reads
        reads += 1
        return clock.utcnow() if reads < 4 else created

    monkeypatch.setattr(store, "_now", rewinding_time)
    with pytest.raises(ValueError):
        await value.run_due()
    assert (await store.review_jobs("local-spot"))[0].status == "pending"
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 1


@pytest.mark.asyncio
async def test_stopping_old_worker_cannot_clear_a_concurrently_started_new_worker(
    context, monkeypatch
):
    value, _, _, _ = await service(context)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def gated_poll():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return ()

    monkeypatch.setattr(value, "run_due", gated_poll)
    await value.start()
    await entered.wait()
    old = value._task

    async def restart_after_old_worker():
        await old
        await value.start()
        return value._task

    restarted = asyncio.create_task(restart_after_old_worker())
    await asyncio.sleep(0)
    stopping = asyncio.create_task(value.stop())
    await value._stopping.wait()
    release.set()
    new_worker = await restarted
    try:
        await stopping
        assert value._task is new_worker and value.running
    finally:
        value._stopping.set()
        await new_worker
        await value.stop()
