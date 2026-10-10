"""Imported exchange facts stay distinct from explicit, append-only attribution."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.domain.account import ObservedTrade, TradeBatch
from agent_platform.domain.events import JournalEvent
from agent_platform.domain.reviews import TradeAttribution
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.adapters.test_sqlite_decisions import completion, request
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_feedback import service as feedback_service
from tests.domain.test_decisions import NOW

context = _context


async def setup(context, *, trade_at=NOW, published_at=None):
    _, _, decisions, clock = await feedback_service(context, published=published_at is None)
    if published_at is not None:
        clock.advance_to(published_at)
        await decisions.claim(request(context[-1]))
        await decisions.finish("request-1", completion(context[-1], at=published_at))
    trade = ObservedTrade(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trade_id="trade-1",
        order_id="order-1",
        side="buy",
        price="60000",
        quantity="0.01",
        fee="0.6",
        fee_asset="USDT",
        quote_quantity="600",
        executed_at=trade_at,
    )
    batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT", trades=(trade,))
    await context[1].ingest(
        batch,
        context[-1],
        JournalEvent(
            event_id="import-trade",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW,
        ),
    )
    adapter = importlib.import_module("agent_platform.adapters.sqlite.attribution")
    application = importlib.import_module("agent_platform.application.attribution")
    store = adapter.SqliteAttributionStore(context[0], clock=clock)
    value = application.AttributionService(store=store, clock=clock, account_ref="local-spot")
    return value, store, trade


def change(identity="change-1", revision=1, *, agent=True, **updates):
    domain = importlib.import_module("agent_platform.domain.attribution")
    attribution = TradeAttribution(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trade_id="trade-1",
        original_author="agent" if agent else "human",
        final_decision_maker="human",
        recommendation_id="advice-1" if agent else None,
        user_confirmed=True,
    )
    attribution = attribution.model_copy(update=updates)
    return domain.AttributionChange(
        operation_id=identity,
        attribution=attribution,
        expected_revision=revision,
        explanation="用户明确确认成交归属",
        recorded_at=NOW,
    )


@pytest.mark.asyncio
async def test_import_stays_unclassified_until_explicit_confirmation(context):
    value, _, trade = await setup(context)
    candidate = change()
    previous = await context[1].load(candidate.attribution.aggregate_id)
    assert previous.revision == 1 and previous.state.original_author == "unclassified"
    receipt = await value.record(candidate)
    state = await context[1].load(candidate.attribution.aggregate_id)
    assert state.revision == receipt.revision == 2
    assert state.state.original_author == "agent"
    assert state.state.final_decision_maker == state.state.executor == "human"
    assert (await context[1].observed_trades("local-spot", "BTCUSDT")) == (trade,)
    assert (await context[1].load("advice-1")).state.original_author == "agent"


@pytest.mark.asyncio
async def test_correction_retains_prior_attribution_and_exact_reopen_retry(context):
    value, store, _ = await setup(context)
    first = await value.record(change())
    corrected = change("change-2", 2, agent=False)
    receipt = await value.record(corrected)
    assert receipt.revision == 3 and receipt.attribution.original_author == "human"
    reopened = type(store)(context[0], clock=value.clock)
    assert await reopened.attribution_change("change-1", account_ref="local-spot") == first
    value.store = reopened
    assert await value.record(corrected) == receipt
    assert (await context[1].load(corrected.attribution.aggregate_id)).revision == 3
    with pytest.raises(RequestIdentityConflict):
        await value.record(corrected.model_copy(update={"explanation": "不同说明"}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"trade_id": "absent"},
        {"account_ref": "another"},
        {"symbol": "ETHUSDT"},
        {"market_type": "futures"},
        {"recommendation_id": "absent"},
        {"user_confirmed": False},
        {"final_decision_maker": "agent"},
        {"original_author": "human"},
    ],
)
async def test_invalid_or_unconfirmed_attribution_never_changes_state(context, updates):
    value, _, _ = await setup(context)
    with pytest.raises(ValueError):
        await value.record(change(**updates))
    assert (await context[1].load(change().attribution.aggregate_id)).revision == 1


@pytest.mark.asyncio
async def test_stale_revision_and_competing_corrections_cannot_overwrite(context):
    value, store, _ = await setup(context)
    second = type(value)(
        store=type(store)(context[0], clock=value.clock),
        clock=value.clock,
        account_ref="local-spot",
    )
    results = await asyncio.gather(
        value.record(change()),
        second.record(change("change-2", agent=False)),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert any(isinstance(result, RevisionConflict) for result in results)
    assert (await context[1].load(change().attribution.aggregate_id)).revision == 2


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_attribution_and_operation_receipt(context):
    value, store, _ = await setup(context)
    with sqlite3.connect(context[0]) as connection:
        connection.execute("""CREATE TRIGGER reject_classification BEFORE INSERT ON journal_events
            WHEN NEW.kind='attribution_recorded'
            BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END;""")
    with pytest.raises(PersistenceUnavailable):
        await value.record(change())
    assert (await context[1].load(change().attribution.aggregate_id)).revision == 1
    assert await store.attribution_change("change-1", account_ref="local-spot") is None


@pytest.mark.asyncio
async def test_event_only_attribution_does_not_count_as_committed_correction(context):
    value, store, _ = await setup(context)
    candidate = change()
    await context[1].append(
        JournalEvent(
            event_id=store.attribution_identity("change-1", "fact"),
            aggregate_id=candidate.attribution.aggregate_id,
            kind="attribution_recorded",
            payload=candidate.attribution,
            occurred_at=NOW,
        )
    )
    with pytest.raises(EventIdentityConflict):
        await value.record(candidate)
    assert (await context[1].load(candidate.attribution.aggregate_id)).revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["previous_day", "before_publication"])
async def test_source_attribution_requires_advice_published_before_execution(context, case):
    value, _, _ = await setup(
        context,
        trade_at=NOW - timedelta(days=1) if case == "previous_day" else NOW,
        published_at=NOW + timedelta(seconds=2) if case == "before_publication" else None,
    )
    candidate = change().model_copy(update={"recorded_at": value.clock.utcnow()})
    with pytest.raises(ValueError):
        await value.record(candidate)
    assert (await context[1].load(candidate.attribution.aggregate_id)).revision == 1


@pytest.mark.asyncio
async def test_attribution_marker_cannot_bypass_recommendation_provenance(context):
    from agent_platform.domain.events import StateRecord

    value, store, _ = await setup(context)
    candidate = change(recommendation_id="missing-advice")
    state = StateRecord(
        key=candidate.attribution.aggregate_id,
        revision=2,
        state_type="attribution",
        state=candidate.attribution,
        updated_at=NOW,
    )
    await context[1].save(
        state,
        1,
        JournalEvent(
            event_id=store.attribution_identity("change-1", "attribution"),
            aggregate_id=state.key,
            kind="state_changed",
            payload=state,
            occurred_at=NOW,
        ),
    )
    marker = StateRecord(
        key=candidate.aggregate_id,
        revision=1,
        state_type="attribution_change",
        state=candidate,
        updated_at=NOW,
    )
    await context[1].save(
        marker,
        0,
        JournalEvent(
            event_id=store.attribution_identity("change-1", "state"),
            aggregate_id=marker.key,
            kind="state_changed",
            payload=marker,
            occurred_at=NOW,
        ),
    )
    await context[1].append(
        JournalEvent(
            event_id=store.attribution_identity("change-1", "fact"),
            aggregate_id=state.key,
            kind="attribution_recorded",
            payload=candidate.attribution,
            occurred_at=NOW,
        )
    )
    with pytest.raises(ValueError):
        await value.record(candidate)
