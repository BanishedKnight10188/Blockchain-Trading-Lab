"""Persistent advice is projected against current cached evidence without network IO."""

import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.decisions import SqliteDecisionStore
from agent_platform.domain.decisions import DecisionTrigger, RecommendationStatus
from agent_platform.domain.events import SessionJournalEvent
from agent_platform.domain.overview import OverviewFrame
from agent_platform.domain.sessions import TradingStyle
from agent_platform.domain.sync import SyncReport
from agent_platform.runtime.latest import LatestOverview
from tests.adapters.test_sqlite_decisions import completion, refresh, request, snapshot
from tests.adapters.test_sqlite_decisions import context as _context
from tests.domain.test_decisions import NOW

context = _context


async def assembly(context):
    path, facts, sessions, _, account = context
    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", market_source="fake", account_source="fake")
    value = snapshot(account)
    await cache.publish(
        OverviewFrame(
            mode="fake",
            market_source="fake",
            account_source="fake",
            captured_at=NOW,
            market=value.market,
            features=value.features,
            sync=SyncReport(
                account=account,
                position=value.position,
                attempted_at=NOW,
                next_attempt_at=NOW + timedelta(seconds=15),
                next_cursor={},
            ),
        )
    )
    factory_module = importlib.import_module("agent_platform.application.snapshots")
    query_module = importlib.import_module("agent_platform.application.advice_queries")
    factory = factory_module.SnapshotFactory(
        sessions, facts, cache, clock, account_ref="local-spot"
    )
    decisions = SqliteDecisionStore(path, clock=clock)
    query = query_module.AdviceQueryService(sessions, decisions, factory, clock)
    return factory, query, decisions, cache, clock


@pytest.mark.asyncio
async def test_snapshot_uses_authoritative_balances_and_distinct_identity(context):
    factory, _, _, _, _ = await assembly(context)
    trigger = request(context[-1]).snapshot.trigger
    first, second = await factory.for_trigger(trigger), await factory.for_trigger(trigger)
    assert first.snapshot_id != second.snapshot_id
    assert first.account == context[-1]
    assert first.position.quantity == 0 and first.position.cost_status == "unknown"
    assert first.style.strength == 67 and first.session_revision == 2
    assert first.features.snapshot_id in first.evidence_ids
    current = await factory.capture(request(context[-1]))
    assert current.snapshot_id != request(context[-1]).snapshot.snapshot_id
    assert set(request(context[-1]).snapshot.evidence_ids) <= set(current.evidence_ids)


@pytest.mark.asyncio
async def test_snapshot_missing_future_stale_or_wrong_scope_closes(context):
    factory, _, _, cache, clock = await assembly(context)
    frame = await cache.latest()
    await cache.publish(frame.model_copy(update={"account_error": "persistence"}))
    assert await factory.for_trigger(request(context[-1]).snapshot.trigger) is None
    await cache.publish(frame)
    clock.advance_to(NOW + timedelta(seconds=6))
    assert await factory.for_trigger(request(context[-1]).snapshot.trigger) is None
    other = DecisionTrigger(
        trigger_id="other",
        session_id="other",
        kind="manual",
        requested_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    assert await factory.for_trigger(other) is None


@pytest.mark.asyncio
async def test_no_request_is_explicit_unavailable(context):
    _, query, _, _, _ = await assembly(context)
    view = await query.current()
    assert view.status == "unavailable" and view.reasons == ("no_decision",)
    assert view.action is None and view.quantity is None


@pytest.mark.asyncio
async def test_pending_request_and_expired_unfinished_work_are_distinct(context):
    _, query, decisions, _, clock = await assembly(context)
    await decisions.claim(request(context[-1]))
    assert (await query.current()).status == "pending"
    assert (await query.current()).expires_at == NOW + timedelta(seconds=15)
    clock.advance_to(NOW + timedelta(seconds=15))
    view = await query.current()
    assert view.status == "unavailable" and view.reasons == ("unfinished_request",)


@pytest.mark.asyncio
async def test_published_view_preserves_style_author_and_privacy(context):
    _, query, decisions, _, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    view = await query.current()
    assert view.status == "published" and view.action == "hold"
    assert view.original_author == "agent" and view.style_strength == 67
    assert view.style_revision == 1 and view.policy_version == "style-v1"
    assert view.evidence_as_of == NOW and view.source == "fake"
    wire = view.model_dump_json()
    assert "local-spot" not in wire and "account_ref" not in wire
    assert "balances" not in wire and "snapshot-1" not in wire


@pytest.mark.asyncio
async def test_published_view_checks_quote_age_without_refresh(context):
    _, query, decisions, _, clock = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    clock.advance_to(NOW + timedelta(seconds=6))
    view = await query.current()
    assert view.status == "unavailable" and "current_evidence_unavailable" in view.reasons
    assert view.action is None
    # Reading cannot rewrite the audited publication or turn stale data into HOLD.
    assert (await decisions.decision("request-1")).result.status == "published"


@pytest.mark.asyncio
async def test_style_change_supersedes_historical_publication(context):
    _, query, decisions, _, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    _, _, sessions, running, _ = context
    changed = running.change_style(TradingStyle(strength=3), NOW)
    await sessions.save(
        changed,
        2,
        SessionJournalEvent(
            event_id="changed",
            kind="style_changed",
            session=changed,
            occurred_at=NOW,
        ),
    )
    view = await query.current()
    assert view.status == "superseded" and view.action is None
    assert view.style_strength == 67 and view.style_revision == 1


@pytest.mark.asyncio
async def test_pause_supersedes_historical_publication(context):
    _, query, decisions, _, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    _, _, sessions, running, _ = context
    paused = running.transition("paused", NOW)
    await sessions.save(
        paused,
        2,
        SessionJournalEvent(
            event_id="paused",
            kind="state_changed",
            session=paused,
            occurred_at=NOW,
        ),
    )
    assert (await query.current()).status == "superseded"


@pytest.mark.asyncio
async def test_unreadable_advice_does_not_claim_known_zero_or_no_model_usage(context):
    from agent_platform.application.queries import QueryService
    from agent_platform.ports.sessions import PersistenceUnavailable

    _, advice, _, cache, clock = await assembly(context)

    async def unavailable(*args):
        raise PersistenceUnavailable("must not reflect")

    advice.current = unavailable
    view = await QueryService(cache, clock, advice=advice).overview()
    assert view.advice.usage_status == "unavailable" and view.advice.usage is None
    assert view.advice.reasons == ("persistence",)


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [14, 61])
async def test_overview_uses_final_clock_after_persistent_advice_reads(context, delay):
    from agent_platform.application.queries import QueryService

    _, advice, decisions, cache, clock = await assembly(context)
    await decisions.claim(request(context[-1]))
    read = decisions.latest_decision

    async def delayed_read(session_id):
        clock.advance_to(NOW + timedelta(seconds=delay))
        return await read(session_id)

    decisions.latest_decision = delayed_read
    view = await QueryService(cache, clock, advice=advice).overview()
    assert view.generated_at == clock.utcnow()
    assert view.market.status == "stale" and view.market.book_status == "stale"
    if delay == 14:
        assert view.advice.status == "pending"
        assert (view.advice.expires_at - view.generated_at).total_seconds() == 1
    else:
        assert view.account.status == "stale" and view.market.feature_status == "stale"
        assert view.advice.status == "unavailable"


@pytest.mark.asyncio
async def test_balance_change_supersedes_without_model_or_network(context):
    _, query, decisions, _, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    await refresh(context[1], context[-1], free="900")
    assert (await query.current()).status == "superseded"


@pytest.mark.asyncio
async def test_expired_advice_stays_expired_with_new_current_quotes(context):
    _, query, decisions, _, clock = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    clock.advance_to(NOW + timedelta(seconds=60))
    view = await query.current()
    assert view.status == "expired" and view.action is None


@pytest.mark.asyncio
async def test_latest_claim_order_survives_reopen_and_pending_masks_old_advice(context):
    _, query, decisions, _, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    data = request(context[-1]).model_dump()
    data.update(request_id="request-2")
    await decisions.claim(type(request(context[-1]))(**data))
    reopened = SqliteDecisionStore(context[0], clock=FakeClock(NOW))
    latest = await reopened.latest_decision("session-1")
    assert latest.request.request_id == "request-2" and latest.completion is None
    assert await reopened.latest_decision("other-session") is None
    assert (await query.current()).status == "pending"


@pytest.mark.asyncio
async def test_account_changes_between_reads_cannot_show_previous_advice(context):
    factory, query, decisions, cache, clock = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    read_account = factory.account
    calls = 0

    async def racing_account():
        nonlocal calls
        value = await read_account()
        calls += 1
        if calls == 1:
            await refresh(context[1], context[-1], free="900")
            updated = await read_account()
            frame = await cache.latest()
            clock.advance_to(updated.as_of)
            await cache.publish(
                frame.model_copy(
                    update={
                        "captured_at": updated.as_of,
                        "sync": frame.sync.model_copy(
                            update={"account": updated, "attempted_at": updated.as_of}
                        ),
                    }
                )
            )
        return value

    factory.account = racing_account
    assert (await query.current()).status == "superseded"


@pytest.mark.asyncio
async def test_await_crossing_advice_ttl_cannot_keep_publication_current(context):
    factory, query, decisions, _, clock = await assembly(context)
    await decisions.claim(request(context[-1]))
    value = completion(context[-1])
    advice = value.result.recommendation.model_copy(
        update={"expires_at": NOW + timedelta(seconds=2)}
    )
    value = value.model_copy(
        update={"result": value.result.model_copy(update={"recommendation": advice})}
    )
    await decisions.finish("request-1", value)
    read_account = factory.account

    async def slow_account():
        clock.advance_to(NOW + timedelta(seconds=3))
        return await read_account()

    factory.account = slow_account
    assert (await query.current()).status == "expired"


@pytest.mark.asyncio
async def test_different_account_with_same_revision_and_balances_is_not_original_scope(context):
    from agent_platform.domain.account import TradeBatch
    from agent_platform.domain.events import JournalEvent

    factory, query, decisions, cache, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    await decisions.finish("request-1", completion(context[-1]))
    account = context[-1].model_copy(update={"account_ref": "other-spot", "account_revision": 0})
    batch = TradeBatch(account_ref="other-spot", symbol="BTCUSDT")
    imported = await context[1].ingest(
        batch,
        account,
        JournalEvent(
            event_id="other-account",
            aggregate_id="other-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW,
        ),
    )
    frame = await cache.latest()
    await cache.publish(
        frame.model_copy(
            update={
                "sync": frame.sync.model_copy(update={"account": imported.account}),
            }
        )
    )
    factory.account_ref = "other-spot"
    assert (await query.current()).status == "superseded"


@pytest.mark.asyncio
async def test_committed_superseded_result_cannot_return_to_published(context):
    _, query, decisions, _, _ = await assembly(context)
    await decisions.claim(request(context[-1]))
    value = completion(context[-1])
    advice = value.result.recommendation.model_copy(
        update={"status": RecommendationStatus.SUPERSEDED}
    )
    result = value.result.model_copy(
        update={
            "status": "superseded",
            "reasons": ("session_changed",),
            "recommendation": advice,
        }
    )
    await decisions.finish("request-1", value.model_copy(update={"result": result}))
    assert (await query.current()).status == "superseded"
