"""Frozen imports, atomic rule reviews and durable immutable version chains."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.account import CostStatus, TradeBatch
from agent_platform.domain.events import JournalEvent
from agent_platform.ports.persistence import RequestIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_trade_groups import fill
from tests.domain.test_decisions import NOW

context = _context


async def service(context):
    batch = TradeBatch(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trades=(
            fill("1", "buy", "1", "100"),
            fill("2", "sell", "1", "110", second=1),
        ),
    )
    await context[1].ingest(
        batch,
        context[-1],
        JournalEvent(
            event_id="review-trades",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW + timedelta(seconds=2),
        ),
    )
    clock = FakeClock(NOW + timedelta(seconds=10))
    adapter = importlib.import_module("agent_platform.adapters.sqlite.reviews")
    application = importlib.import_module("agent_platform.application.reviews")
    store = adapter.SqliteReviewStore(context[0], clock=clock)
    return (
        application.ReviewService(store=store, clock=clock, account_ref="local-spot"),
        store,
        clock,
    )


@pytest.mark.asyncio
async def test_group_freezes_real_local_facts_and_incomplete_basis(context):
    value, store, clock = await service(context)
    group = await value.create_group("group-1", clock.utcnow())
    assert group.group.trade_group_id == "group-1" and len(group.group.trades) == 2
    assert group.group.cost_status == "partial" and group.facts.realized_pnl_quote is None
    assert group.coverage is None
    assert await store.review_group("group-1", account_ref="local-spot") == group
    assert await store.review_group("group-1", account_ref="foreign") is None


@pytest.mark.asyncio
async def test_group_retry_returns_original_and_other_cutoff_conflicts(context):
    value, _, clock = await service(context)
    original = await value.create_group("group-1", clock.utcnow())
    clock.advance_to(clock.utcnow() + timedelta(seconds=1))
    assert await value.create_group("group-1", original.facts.data_cutoff) == original
    with pytest.raises(RequestIdentityConflict):
        await value.create_group("group-1", clock.utcnow())


@pytest.mark.asyncio
async def test_initial_review_and_followup_keep_old_facts_and_versions(context):
    value, store, clock = await service(context)
    group = await value.create_group("group-1", clock.utcnow())
    initial = await value.generate("group-1", clock.utcnow(), "initial")
    frozen = initial.model_dump_json()
    clock.advance_to(NOW + timedelta(hours=1))
    followup = await value.generate("group-1", clock.utcnow(), "followup")
    assert initial.revision == 1 and followup.revision == 2
    assert followup.parent_review_id == initial.review_id and followup.is_retrospective
    assert initial.model_dump_json() == frozen
    records = await store.review_versions("group-1", account_ref="local-spot")
    assert tuple(item.review for item in records) == (initial, followup)
    assert all(item.facts == group.facts for item in records)
    assert all(
        not item.review.model_participated and item.review.realized_pnl_usd is None
        for item in records
    )
    assert all(item.review.evidence_ids == ("trade:1", "trade:2") for item in records)


@pytest.mark.asyncio
async def test_rule_review_preserves_unclassified_author_and_does_not_change_style(context):
    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    session = await context[2].active()
    review = await value.generate("group-1", clock.utcnow(), "initial")
    record = (await store.review_versions("group-1", account_ref="local-spot"))[0]
    assert "成本" in review.explanation and "模型" in review.explanation
    assert all(item.original_author == "unclassified" for item in record.attributions)
    assert await context[2].active() == session


@pytest.mark.asyncio
async def test_exact_review_retry_and_reopen_return_same_version(context):
    value, store, clock = await service(context)
    cutoff = clock.utcnow()
    await value.create_group("group-1", cutoff)
    first = await value.generate("group-1", cutoff, "initial")
    clock.advance_to(cutoff + timedelta(seconds=2))
    assert await value.generate("group-1", cutoff, "initial") == first
    application = importlib.import_module("agent_platform.application.reviews")
    reopened = application.ReviewService(
        store=type(store)(context[0], clock=clock), clock=clock, account_ref="local-spot"
    )
    assert await reopened.generate("group-1", cutoff, "initial") == first
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 1


@pytest.mark.asyncio
async def test_concurrent_identical_review_requests_produce_one_version(context):
    value, store, clock = await service(context)
    cutoff = clock.utcnow()
    await value.create_group("group-1", cutoff)
    other = type(value)(
        store=type(store)(context[0], clock=clock), clock=clock, account_ref="local-spot"
    )
    results = await asyncio.gather(
        *(item.generate("group-1", cutoff, "initial") for item in (value, other))
    )
    assert results[0] == results[1]
    assert len(await store.review_versions("group-1", account_ref="local-spot")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,offset", [("followup", 0), ("initial", 1), ("manual", -11)])
async def test_missing_parent_future_or_prior_cutoff_rejected(context, kind, offset):
    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    with pytest.raises(ValueError):
        await value.generate("group-1", clock.utcnow() + timedelta(seconds=offset), kind)
    assert await store.review_versions("group-1", account_ref="local-spot") == ()


@pytest.mark.asyncio
async def test_first_manual_review_allowed_but_second_initial_rejected(context):
    value, _, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    first = await value.generate("group-1", clock.utcnow(), "manual")
    assert first.revision == 1
    clock.advance_to(clock.utcnow() + timedelta(seconds=1))
    with pytest.raises(ValueError):
        await value.generate("group-1", clock.utcnow(), "initial")


@pytest.mark.asyncio
async def test_later_import_does_not_rewrite_frozen_review_group(context):
    value, store, clock = await service(context)
    group = await value.create_group("group-1", clock.utcnow())
    trade = fill("3", "buy", "1", "120", second=11)
    batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT", trades=(trade,))
    await store.ingest(
        batch,
        context[-1],
        JournalEvent(
            event_id="later-import",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW + timedelta(seconds=12),
        ),
    )
    clock.advance_to(NOW + timedelta(seconds=20))
    await value.generate("group-1", clock.utcnow(), "initial")
    assert await store.review_group("group-1", account_ref="local-spot") == group
    assert (
        len((await store.review_versions("group-1", account_ref="local-spot"))[0].facts.matches)
        == 1
    )


@pytest.mark.asyncio
async def test_review_audit_failure_rolls_back_version_and_current_projection(context, monkeypatch):
    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    append = store._append

    def failing(connection, event):
        if event.kind == "review_recorded":
            raise OSError("offline injected audit failure")
        return append(connection, event)

    monkeypatch.setattr(store, "_append", failing)
    with pytest.raises(PersistenceUnavailable):
        await value.generate("group-1", clock.utcnow(), "initial")
    assert await store.review_versions("group-1", account_ref="local-spot") == ()
    assert await store.load("group-1") is None


@pytest.mark.asyncio
async def test_group_does_not_accept_report_or_foreign_scope_as_exchange_history(context):
    value, _, clock = await service(context)
    foreign = type(value)(store=value.store, clock=clock, account_ref="another")
    with pytest.raises(ValueError):
        await foreign.create_group("group-1", clock.utcnow())
    with pytest.raises(ValueError):
        await value.create_group("group-1", clock.utcnow() + timedelta(seconds=1))


@pytest.mark.asyncio
async def test_classified_attribution_is_frozen_and_later_correction_creates_new_evidence(context):
    from agent_platform.application.attribution import AttributionService
    from agent_platform.domain.attribution import AttributionChange
    from agent_platform.domain.reviews import TradeAttribution

    value, store, clock = await service(context)
    await value.create_group("group-1", clock.utcnow())
    attribution = AttributionService(store=store, clock=clock, account_ref="local-spot")
    original = TradeAttribution(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trade_id="1",
        original_author="human",
        final_decision_maker="human",
        user_confirmed=True,
    )
    await attribution.record(
        AttributionChange(
            operation_id="review-attribution",
            attribution=original,
            expected_revision=1,
            explanation="本人独立决策",
            recorded_at=clock.utcnow(),
        )
    )
    initial = await value.generate("group-1", clock.utcnow(), "initial")
    before = (await store.review_versions("group-1", account_ref="local-spot"))[0]
    assert (
        before.attributions[0].original_author == "human" and before.attribution_revisions[0] == 2
    )
    clock.advance_to(clock.utcnow() + timedelta(seconds=1))
    await attribution.record(
        AttributionChange(
            operation_id="review-correction",
            attribution=original,
            expected_revision=2,
            explanation="追加说明",
            recorded_at=clock.utcnow(),
        )
    )
    later = await value.generate("group-1", clock.utcnow(), "followup")
    records = await store.review_versions("group-1", account_ref="local-spot")
    assert records[0] == before and records[1].attribution_revisions[0] == 3
    assert later.parent_review_id == initial.review_id


@pytest.mark.asyncio
async def test_event_only_review_fact_cannot_count_as_completed_version(context):
    from agent_platform.domain.review_records import review_identity
    from agent_platform.domain.reviews import ReviewRevision
    from agent_platform.ports.persistence import EventIdentityConflict

    value, store, clock = await service(context)
    group = await value.create_group("group-1", clock.utcnow())
    identity = review_identity("group-1", clock.utcnow(), "initial")
    fact = ReviewRevision(
        review_id=identity,
        trade_group_id="group-1",
        revision=1,
        kind="initial",
        data_cutoff=clock.utcnow(),
        generated_at=clock.utcnow(),
        evidence_ids=("trade:1", "trade:2"),
        explanation="孤立事实",
        cost_status=group.facts.cost_status,
    )
    await store.append(
        JournalEvent(
            event_id=identity + ":fact",
            aggregate_id="group-1",
            kind="review_recorded",
            payload=fact,
            occurred_at=clock.utcnow(),
        )
    )
    with pytest.raises(EventIdentityConflict):
        await value.generate("group-1", clock.utcnow(), "initial")
    assert await store.review_versions("group-1", account_ref="local-spot") == ()


@pytest.mark.asyncio
async def test_group_audit_failure_rolls_back_entire_frozen_group(context, monkeypatch):
    value, store, clock = await service(context)
    append = store._append

    def failing(connection, event):
        if event.kind == "review_group_frozen":
            raise OSError("offline group audit failure")
        return append(connection, event)

    monkeypatch.setattr(store, "_append", failing)
    with pytest.raises(PersistenceUnavailable):
        await value.create_group("group-1", clock.utcnow())
    assert await store.review_group("group-1", account_ref="local-spot") is None


@pytest.mark.asyncio
async def test_group_cannot_claim_known_cost_by_forging_completeness(context):
    value, store, clock = await service(context)
    group = await value.create_group("group-1", clock.utcnow())
    with pytest.raises(ValueError):
        await store.freeze_review_group(
            group.model_copy(
                update={
                    "group": group.group.model_copy(
                        update={"trade_group_id": "fake-group", "cost_status": CostStatus.KNOWN}
                    )
                }
            )
        )


@pytest.mark.asyncio
async def test_group_import_race_rejects_stale_frozen_history(context, monkeypatch):
    value, store, clock = await service(context)
    freeze = store.freeze_review_group

    async def import_then_freeze(group):
        batch = TradeBatch(
            account_ref="local-spot",
            symbol="BTCUSDT",
            trades=(fill("3", "buy", "1", "100", second=2),),
        )
        await store.ingest(
            batch,
            context[-1],
            JournalEvent(
                event_id="racing-import",
                aggregate_id="local-spot",
                kind="trades_imported",
                payload=batch,
                occurred_at=clock.utcnow(),
            ),
        )
        return await freeze(group)

    monkeypatch.setattr(store, "freeze_review_group", import_then_freeze)
    with pytest.raises(ValueError):
        await value.create_group("group-1", clock.utcnow())
    assert await store.review_group("group-1", account_ref="local-spot") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed_generation", [False, True])
async def test_review_cutoff_freezes_attribution_before_later_correction(
    context, delayed_generation
):
    from agent_platform.domain.attribution import AttributionChange
    from agent_platform.domain.review_records import ReviewProposal
    from agent_platform.domain.reviews import TradeAttribution

    value, store, clock = await service(context)
    cutoff = clock.utcnow()
    await value.create_group("group-1", cutoff)
    proposal = ReviewProposal(
        trade_group_id="group-1",
        account_ref="local-spot",
        kind="manual",
        data_cutoff=cutoff,
        generated_at=cutoff,
        explanation="按截止时间核对",
    )
    clock.advance_to(cutoff + timedelta(seconds=1))
    attribution = TradeAttribution(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trade_id="1",
        original_author="human",
        final_decision_maker="human",
        user_confirmed=True,
    )
    await store.record_attribution(
        AttributionChange(
            operation_id="after-review-cutoff",
            attribution=attribution,
            expected_revision=1,
            explanation="稍后确认",
            recorded_at=clock.utcnow(),
        )
    )
    if delayed_generation:
        proposal = proposal.model_copy(update={"generated_at": clock.utcnow()})
    record = await store.record_review(proposal)
    assert record.attributions[0].original_author == "unclassified"
    assert record.attribution_revisions[0] == 1
    assert await store.record_review(proposal) == record
    assert (await store.review_versions("group-1", account_ref="local-spot"))[0] == record


@pytest.mark.asyncio
async def test_group_cutoff_cannot_precede_receipt_of_imported_facts(context):
    value, _, _ = await service(context)
    with pytest.raises(ValueError):
        await value.create_group("group-1", NOW)


@pytest.mark.asyncio
async def test_generic_complete_phases_cannot_regress_review_cutoff(context):
    from agent_platform.domain.events import StateRecord
    from agent_platform.domain.review_records import review_identity
    from agent_platform.domain.reviews import ReviewKind
    from agent_platform.ports.persistence import EventIdentityConflict

    value, store, clock = await service(context)
    cutoff = clock.utcnow()
    await value.create_group("group-1", cutoff)
    clock.advance_to(cutoff + timedelta(seconds=10))
    initial = await value.generate("group-1", clock.utcnow(), "initial")
    original = (await store.review_versions("group-1", account_ref="local-spot"))[0]
    clock.advance_to(clock.utcnow() + timedelta(seconds=1))
    key = review_identity("group-1", cutoff, "followup")
    altered_review = initial.model_copy(
        update=dict(
            review_id=key,
            kind=ReviewKind.FOLLOWUP,
            revision=2,
            parent_review_id=initial.review_id,
            data_cutoff=cutoff,
            generated_at=clock.utcnow(),
        )
    )
    altered_record = original.model_copy(update={"review": altered_review})
    for state, prior, phase in (
        (
            StateRecord(
                key=key,
                revision=1,
                state_type="review_record",
                state=altered_record,
                updated_at=clock.utcnow(),
            ),
            0,
            "record",
        ),
        (
            StateRecord(
                key="group-1",
                revision=2,
                state_type="review",
                state=altered_review,
                updated_at=clock.utcnow(),
            ),
            1,
            "current",
        ),
    ):
        await store.save(
            state,
            prior,
            JournalEvent(
                event_id=key + ":" + phase,
                aggregate_id=state.key,
                kind="state_changed",
                payload=state,
                occurred_at=clock.utcnow(),
            ),
        )
    await store.append(
        JournalEvent(
            event_id=key + ":fact",
            aggregate_id="group-1",
            kind="review_recorded",
            payload=altered_review,
            occurred_at=clock.utcnow(),
        )
    )
    with pytest.raises(EventIdentityConflict):
        await store.review_versions("group-1", account_ref="local-spot")


@pytest.mark.asyncio
async def test_only_trades_inside_cutoff_count_toward_history_limit(context):
    value, store, clock = await service(context)
    cutoff = clock.utcnow()
    trades = tuple(fill(str(i + 3), "buy", "1", "100", second=20) for i in range(4095))
    batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT", trades=trades)
    await store.ingest(
        batch,
        context[-1],
        JournalEvent(
            event_id="large-future-history",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW + timedelta(seconds=25),
        ),
    )
    clock.advance_to(NOW + timedelta(seconds=25))
    history = await store.review_history("local-spot", cutoff)
    assert len(history.trades) == 2
    group = await value.create_group("group-1", cutoff)
    assert len(group.group.trades) == 2
    with pytest.raises(ValueError):
        await store.review_history("local-spot", clock.utcnow())


@pytest.mark.asyncio
async def test_review_version_limit_rejects_before_changing_current_projection(context):
    from agent_platform.domain.events import StateRecord
    from agent_platform.domain.review_records import ReviewProposal, review_identity
    from agent_platform.domain.reviews import ReviewKind, ReviewRevision

    value, store, clock = await service(context)
    base = clock.utcnow()
    await value.create_group("group-1", base)
    initial = await value.generate("group-1", base, "initial")
    template = (await store.review_versions("group-1", account_ref="local-spot"))[0]
    parent = initial.review_id
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index in range(2, 4097):
            cutoff = base + timedelta(microseconds=index - 1)
            key = review_identity("group-1", cutoff, ReviewKind.MANUAL)
            review = ReviewRevision.model_validate(
                {
                    **initial.model_dump(),
                    "review_id": key,
                    "revision": index,
                    "kind": ReviewKind.MANUAL,
                    "parent_review_id": parent,
                    "data_cutoff": cutoff,
                    "generated_at": cutoff,
                }
            )
            record = template.model_copy(update={"review": review})
            store._feedback_state(
                connection,
                StateRecord(
                    key=key, revision=1, state_type="review_record", state=record, updated_at=cutoff
                ),
                key + ":record",
            )
            store._feedback_state(
                connection,
                StateRecord(
                    key="group-1",
                    revision=index,
                    state_type="review",
                    state=review,
                    updated_at=cutoff,
                ),
                key + ":current",
            )
            store._append(
                connection,
                JournalEvent(
                    event_id=key + ":fact",
                    aggregate_id="group-1",
                    kind="review_recorded",
                    payload=review,
                    occurred_at=cutoff,
                ),
            )
            parent = key
    previous = await store.load("group-1")
    clock.advance_to(base + timedelta(microseconds=4096))
    proposal = ReviewProposal(
        trade_group_id="group-1",
        account_ref="local-spot",
        kind="manual",
        data_cutoff=clock.utcnow(),
        generated_at=clock.utcnow(),
        explanation="不能写入超限版本",
    )
    with pytest.raises(ValueError):
        await store.record_review(proposal)
    assert await store.load("group-1") == previous
    assert (await store.review_versions("group-1", account_ref="local-spot", after_revision=4095))[
        0
    ].review.revision == 4096
