"""Feedback is local human intent, with atomic advice state and unchanged original facts."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.domain.decisions import AdvisoryAssessment, DecisionFeedback
from agent_platform.domain.events import JournalEvent
from agent_platform.domain.sessions import TradingStyle
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.adapters.test_sqlite_decisions import completion, request
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_advice_queries import assembly
from tests.domain.test_decisions import NOW

context = _context


def feedback(kind="accepted", identity="feedback-1"):
    return DecisionFeedback(
        feedback_id=identity,
        kind=kind,
        recommendation_id=None if kind == "independent" else "advice-1",
        explanation="用户明确记录的测试想法",
        recorded_at=NOW,
        modified_assessment=AdvisoryAssessment(
            action="hold",
            explanation="我选择继续观察",
            source="human",
        )
        if kind in ("modified", "independent")
        else None,
    )


async def service(context, *, published=True):
    factory, _, decisions, _, clock = await assembly(context)
    adapter = importlib.import_module("agent_platform.adapters.sqlite.feedback")
    application = importlib.import_module("agent_platform.application.feedback")
    store = adapter.SqliteFeedbackStore(context[0], clock=clock)
    if published:
        await decisions.claim(request(context[-1]))
        await decisions.finish("request-1", completion(context[-1]))
    value = application.FeedbackService(
        store=store,
        states=context[1],
        snapshots=factory,
        clock=clock,
        account_ref="local-spot",
    )
    return value, store, decisions, clock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,status",
    [
        ("accepted", "accepted"),
        ("rejected", "rejected"),
        ("modified", "rejected"),
    ],
)
async def test_feedback_and_advice_state_commit_together_without_rewriting_original(
    context, kind, status
):
    value, store, decisions, _ = await service(context)
    original = (await decisions.decision("request-1")).result
    receipt = await value.record(feedback(kind))
    state = await context[1].load("advice-1")
    assert state.revision == 3 and state.state.status == status
    assert state.state.original_author == "agent"
    assert state.state.assessment == original.recommendation.assessment
    assert receipt.feedback.final_decision_maker == "human"
    assert (await decisions.decision("request-1")).result == original
    assert await store.feedback("feedback-1", account_ref="local-spot") == receipt


@pytest.mark.asyncio
async def test_independent_human_idea_needs_no_model_account_balance_or_recommendation(context):
    value, store, _, _ = await service(context, published=False)
    receipt = await value.record(feedback("independent"))
    assert receipt.feedback.modified_assessment.source == "human"
    assert receipt.feedback.recommendation_id is None
    assert await store.feedback("feedback-1", account_ref="local-spot") == receipt
    with sqlite3.connect(context[0]) as connection:
        assert connection.execute("SELECT count(*) FROM budget_requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_feedback_idempotence_survives_reopen_and_changed_inputs_are_rejected(context):
    value, _, _, clock = await service(context)
    original = feedback()
    first = await value.record(original)
    clock.advance_to(NOW + timedelta(seconds=100))
    value, _, _, reopened_clock = await service(context, published=False)
    reopened_clock.advance_to(clock.utcnow())
    repeated = await value.record(original)
    assert repeated == first and (await context[1].load("advice-1")).revision == 3
    changed = original.model_copy(update={"explanation": "不同说明"})
    with pytest.raises(RequestIdentityConflict):
        await value.record(changed)


@pytest.mark.asyncio
async def test_expired_or_nonexistent_advice_cannot_be_accepted(context):
    value, _, _, clock = await service(context)
    clock.advance_to(NOW + timedelta(seconds=6))
    with pytest.raises(ValueError):
        await value.record(feedback())
    assert (await context[1].load("advice-1")).state.status == "published"
    value, _, _, _ = await service(context, published=False)
    missing = feedback().model_copy(update={"recommendation_id": "missing"})
    with pytest.raises(ValueError):
        await value.record(missing)


@pytest.mark.asyncio
async def test_feedback_audit_failure_rolls_back_feedback_and_advice_state(context):
    value, store, _, _ = await service(context)
    with sqlite3.connect(context[0]) as connection:
        connection.execute("""
            CREATE TRIGGER reject_feedback BEFORE INSERT ON journal_events
            WHEN NEW.kind='feedback_recorded'
            BEGIN SELECT RAISE(ABORT, 'simulated feedback audit failure'); END;
        """)
    with pytest.raises(PersistenceUnavailable):
        await value.record(feedback())
    assert await store.feedback("feedback-1", account_ref="local-spot") is None
    assert (await context[1].load("advice-1")).revision == 2


@pytest.mark.asyncio
async def test_event_only_feedback_is_not_a_committed_operation(context):
    value, store, _, _ = await service(context)
    candidate = feedback()
    await context[1].append(
        JournalEvent(
            event_id=store.feedback_identity(candidate.feedback_id, "fact"),
            aggregate_id=candidate.feedback_id,
            kind="feedback_recorded",
            payload=candidate,
            occurred_at=NOW,
        )
    )
    with pytest.raises(EventIdentityConflict):
        await value.record(candidate)
    assert (await context[1].load("advice-1")).revision == 2


@pytest.mark.asyncio
async def test_style_change_between_precheck_and_commit_blocks_acceptance(context):
    from agent_platform.application.sessions import SessionService

    value, store, _, clock = await service(context)
    capture = value.snapshots.capture

    async def changed_style(request):
        current = await capture(request)
        await SessionService(context[2], clock).change_style(
            "session-1",
            TradingStyle(strength=1),
            2,
        )
        return current

    value.snapshots.capture = changed_style
    with pytest.raises(ValueError):
        await value.record(feedback())
    assert await store.feedback("feedback-1", account_ref="local-spot") is None
    assert (await context[1].load("advice-1")).state.status == "published"


@pytest.mark.asyncio
async def test_query_does_not_republish_accepted_or_rejected_completion(context):
    value, _, _, _ = await service(context)
    await value.record(feedback())
    _, query, _, _, _ = await assembly(context)
    view = await query.current()
    assert view.status == "accepted" and view.action is None
    assert view.original_author == "agent"


@pytest.mark.asyncio
async def test_account_change_between_capture_and_write_blocks_feedback(context):
    from agent_platform.domain.account import TradeBatch

    value, store, _, _ = await service(context)
    capture = value.snapshots.capture

    async def changed_account(request):
        current = await capture(request)
        batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT")
        account = context[-1].model_copy(update={"balances": ()})
        await context[1].ingest(
            batch,
            account,
            JournalEvent(
                event_id="changed-account",
                aggregate_id="local-spot",
                kind="trades_imported",
                payload=batch,
                occurred_at=NOW,
            ),
        )
        return current

    value.snapshots.capture = changed_account
    with pytest.raises(ValueError):
        await value.record(feedback())
    assert await store.feedback("feedback-1", account_ref="local-spot") is None
    assert (await context[1].load("advice-1")).revision == 2


@pytest.mark.asyncio
async def test_audit_delay_expiring_quote_rolls_back_all_feedback_writes(context):
    value, store, _, clock = await service(context)

    class SlowAudit(type(store)):
        def _append(self, connection, event):
            receipt = super()._append(connection, event)
            if event.kind == "feedback_recorded":
                clock.advance_to(NOW + timedelta(seconds=6))
            return receipt

    value.store = SlowAudit(context[0], clock=clock)
    with pytest.raises(ValueError):
        await value.record(feedback())
    assert await store.feedback("feedback-1", account_ref="local-spot") is None
    assert (await context[1].load("advice-1")).revision == 2


@pytest.mark.asyncio
async def test_competing_human_responses_only_one_can_finish_advice(context):
    first, store, _, _ = await service(context)
    second, _, _, _ = await service(context, published=False)
    results = await asyncio.gather(
        first.record(feedback()),
        second.record(feedback("rejected", "feedback-2")),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert (await context[1].load("advice-1")).revision == 3
    receipts = [
        await store.feedback(identity, account_ref="local-spot")
        for identity in ("feedback-1", "feedback-2")
    ]
    assert sum(receipt is not None for receipt in receipts) == 1


@pytest.mark.asyncio
async def test_query_rechecks_feedback_committed_during_snapshot_read(context):
    value, _, _, _ = await service(context)
    _, query, _, _, _ = await assembly(context)
    read = query.snapshots.for_trigger

    async def accept_during_read(*args, **kwargs):
        current = await read(*args, **kwargs)
        await value.record(feedback())
        return current

    query.snapshots.for_trigger = accept_during_read
    view = await query.current()
    assert view.status == "accepted" and view.action is None


@pytest.mark.asyncio
async def test_query_closes_advice_when_current_projection_is_missing(context):
    await service(context)
    with sqlite3.connect(context[0]) as connection:
        connection.execute("DELETE FROM domain_states WHERE key='advice-1'")
    _, query, _, _, _ = await assembly(context)
    view = await query.current()
    assert view.status == "unavailable" and view.action is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["future", "long_text", "cross_account", "paper"])
async def test_feedback_rejects_invalid_time_text_and_scope(context, case):
    value, store, _, _ = await service(context)
    candidate = feedback("independent" if case == "paper" else "accepted")
    if case == "future":
        candidate = candidate.model_copy(update={"recorded_at": NOW + timedelta(seconds=1)})
    elif case == "long_text":
        candidate = candidate.model_copy(update={"explanation": "x" * 4097})
    elif case == "cross_account":
        value.account_ref = "another"
    else:
        value.account_ref = "paper:local"
    with pytest.raises(ValueError):
        await value.record(candidate)
    assert await store.feedback("feedback-1", account_ref="local-spot") is None
    assert (await context[1].load("advice-1")).revision == 2


@pytest.mark.asyncio
async def test_separately_written_marker_and_fact_cannot_forge_feedback_completion(context):
    from agent_platform.domain.events import StateRecord
    from agent_platform.domain.feedback import FeedbackRecord

    value, store, _, _ = await service(context)
    candidate = feedback()
    owned = FeedbackRecord(feedback=candidate, account_ref="local-spot")
    state = StateRecord(
        key=owned.aggregate_id, revision=1, state_type="feedback", state=owned, updated_at=NOW
    )
    await context[1].save(
        state,
        0,
        JournalEvent(
            event_id=store.feedback_identity("feedback-1", "state"),
            aggregate_id=state.key,
            kind="state_changed",
            payload=state,
            occurred_at=NOW,
        ),
    )
    await context[1].append(
        JournalEvent(
            event_id=store.feedback_identity("feedback-1", "fact"),
            aggregate_id="feedback-1",
            kind="feedback_recorded",
            payload=candidate,
            occurred_at=NOW,
        )
    )
    with pytest.raises(EventIdentityConflict):
        await value.record(candidate)
    assert (await context[1].load("advice-1")).revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rejected", "modified"])
async def test_nonaccepting_feedback_can_be_recorded_when_quote_is_stale(context, kind):
    value, _, _, clock = await service(context)
    clock.advance_to(NOW + timedelta(seconds=6))
    receipt = await value.record(feedback(kind).model_copy(update={"recorded_at": clock.utcnow()}))
    assert receipt.feedback.kind == kind
    assert (await context[1].load("advice-1")).state.status == "rejected"


@pytest.mark.asyncio
async def test_rejecting_expired_advice_records_human_fact_and_expired_projection(context):
    value, _, _, clock = await service(context)
    clock.advance_to(NOW + timedelta(seconds=61))
    receipt = await value.record(
        feedback("rejected").model_copy(update={"recorded_at": clock.utcnow()})
    )
    assert receipt.feedback.kind == "rejected"
    assert (await context[1].load("advice-1")).state.status == "expired"


@pytest.mark.asyncio
async def test_feedback_commit_cannot_freeze_published_state_as_accepted(context):
    from agent_platform.domain.events import StateRecord
    from agent_platform.domain.feedback import FeedbackRecord

    value, store, _, _ = await service(context)
    previous = await context[1].load("advice-1")
    same_state = previous.model_copy(update={"revision": 3})
    await context[1].save(
        same_state,
        2,
        JournalEvent(
            event_id=store.feedback_identity("feedback-1", "advice"),
            aggregate_id="advice-1",
            kind="state_changed",
            payload=same_state,
            occurred_at=NOW,
        ),
    )
    with pytest.raises(ValueError):
        owned = FeedbackRecord(
            feedback=feedback(),
            account_ref="local-spot",
            recommendation_state=same_state.state,
            recommendation_revision=3,
        )
        marker = StateRecord(
            key=owned.aggregate_id, revision=1, state_type="feedback", state=owned, updated_at=NOW
        )
        await context[1].save(
            marker,
            0,
            JournalEvent(
                event_id=store.feedback_identity("feedback-1", "state"),
                aggregate_id=marker.key,
                kind="state_changed",
                payload=marker,
                occurred_at=NOW,
            ),
        )
        await context[1].append(
            JournalEvent(
                event_id=store.feedback_identity("feedback-1", "fact"),
                aggregate_id="feedback-1",
                kind="feedback_recorded",
                payload=feedback(),
                occurred_at=NOW,
            )
        )
        await value.record(feedback())
