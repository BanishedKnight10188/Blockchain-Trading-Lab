"""Durable claims and atomic publication prevent repeated calls and stale advice."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta
from hashlib import sha256

import pytest
import pytest_asyncio

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.schema import SCHEMA_VERSION
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.adapters.sqlite.store import open_store
from agent_platform.domain.account import AccountSnapshot, TradeBatch
from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.domain.decisions import DecisionResult, DecisionSnapshot, Recommendation
from agent_platform.domain.events import JournalEvent, SessionJournalEvent, StateRecord
from agent_platform.domain.sessions import AgentSession, TradingStyle
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.domain.test_decisions import NOW, recommendation_data, snapshot_data


def modules():
    return (
        importlib.import_module("agent_platform.domain.decision_requests"),
        importlib.import_module("agent_platform.adapters.sqlite.decisions"),
    )


@pytest_asyncio.fixture
async def context(tmp_path):
    path = tmp_path / "agent.sqlite3"
    facts = await open_store(path)
    sessions = SqliteSessionStore(path)
    initial = AgentSession(
        session_id="session-1",
        style={"strength": 67},
        created_at=NOW,
        updated_at=NOW,
    )
    await sessions.create(
        initial,
        SessionJournalEvent(
            event_id="create-session",
            kind="created",
            session=initial,
            occurred_at=NOW,
        ),
    )
    running = initial.transition("running", NOW)
    await sessions.save(
        running,
        1,
        SessionJournalEvent(
            event_id="start-session",
            kind="state_changed",
            session=running,
            occurred_at=NOW,
        ),
    )
    batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT")
    account = AccountSnapshot(
        account_ref="local-spot",
        as_of=NOW,
        balances=({"asset": "USDT", "free": "1000", "locked": "0"},),
    )
    imported = await facts.ingest(
        batch,
        account,
        JournalEvent(
            event_id="account-initial",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW,
        ),
    )
    return path, facts, sessions, running, imported.account


def snapshot(account, identity="snapshot-1", at=NOW):
    value = snapshot_data()
    value.update(snapshot_id=identity, session_revision=2, captured_at=at, account=account)
    value["market"].update(
        as_of=at,
        status="ready",
        latest_received_at=at,
        latest_quote_at=at,
        book_as_of=at,
        book=dict(
            symbol="BTCUSDT",
            bid="60000",
            ask="60001",
            bid_quantity="1",
            ask_quantity="1",
        ),
    )
    value["features"].update(
        as_of=at,
        warmup_ready=True,
        interval_return="0",
        ema_fast="60000",
        ema_slow="60000",
        atr="1",
        vwap="60000",
        volatility="0",
        volume_change="0",
        spread="1",
    )
    return DecisionSnapshot(**value)


def request(account):
    domain, _ = modules()
    return domain.DecisionRequest(
        request_id="request-1",
        snapshot=snapshot(account),
        requested_at=NOW,
        deadline=NOW + timedelta(seconds=15),
        prompt_version="advisory-prompt-v1",
        route=dict(route_id="rule:advisory", kind="rule", purpose="advisory", reason="disabled"),
    )


def completion(account, at=NOW, **changes):
    domain, _ = modules()
    advice = recommendation_data()
    advice.update(account_revision=1, created_at=at, updated_at=at)
    recommendation = Recommendation(**advice).transition("published", at)
    publication = snapshot(account, "publication-1", at)
    result = DecisionResult(
        request_id="request-1",
        snapshot_id="snapshot-1",
        publication_snapshot_id="publication-1",
        status="published",
        recommendation=recommendation,
        risk=dict(snapshot_id="publication-1", outcome="allow", evaluated_at=at),
    )
    return domain.DecisionCompletion(
        result=result,
        publication_snapshot=publication,
        completed_at=at,
        **changes,
    )


@pytest.mark.asyncio
async def test_claim_is_durable_unique_and_never_reclaims_unknown_work(context):
    path, _, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    value = request(account)
    first = await store.claim(value)
    assert first.claimed and first.result is None
    reopened = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    repeat = await reopened.claim(value)
    assert not repeat.claimed and repeat.result is None
    assert (await reopened.decision("request-1")).request == value
    assert (await reopened.decision("missing")) is None
    changed = value.model_dump()
    changed["snapshot"]["style"]["strength"] = 99
    with pytest.raises(RequestIdentityConflict):
        await reopened.claim(type(value)(**changed))


@pytest.mark.asyncio
async def test_two_independent_claimants_have_exactly_one_owner(context):
    path, _, _, _, account = context
    _, adapter = modules()
    stores = [adapter.SqliteDecisionStore(path, clock=FakeClock(NOW)) for _ in range(2)]
    receipts = await asyncio.gather(*(store.claim(request(account)) for store in stores))
    assert sum(item.claimed for item in receipts) == 1


@pytest.mark.asyncio
async def test_publication_snapshot_is_distinct_and_created_audit_precedes_publish(context):
    path, facts, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    value = completion(account)
    result = await store.finish("request-1", value)
    assert result.status == "published" and result.publication_snapshot_id == "publication-1"
    state = await facts.load("advice-1")
    assert state.revision == 2 and state.state.status == "published"
    page = await facts.scan(0, 100)
    advice_events = [item.event for item in page.records if item.event.aggregate_id == "advice-1"]
    assert [item.payload.state.status for item in advice_events] == ["created", "published"]
    assert await store.finish("request-1", value) == result
    assert (await store.claim(request(account))).result == result
    assert len((await facts.scan(0, 100)).records) == len(page.records)


@pytest.mark.asyncio
async def test_style_change_after_read_cannot_publish_old_assessment(context):
    path, facts, sessions, running, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    old_read = completion(account)
    changed = running.change_style(TradingStyle(strength=100), NOW)
    await sessions.save(
        changed,
        2,
        SessionJournalEvent(
            event_id="change-style",
            kind="style_changed",
            session=changed,
            occurred_at=NOW,
        ),
    )
    result = await store.finish("request-1", old_read)
    assert result.status == "superseded" and "session_changed" in result.reasons
    assert result.recommendation.assessment == old_read.result.recommendation.assessment
    assert result.recommendation.original_author == "agent"
    assert (await facts.load("advice-1")).state.status == "superseded"


async def refresh(facts, account, *, free="1000", status="fresh", at=NOW + timedelta(seconds=1)):
    value = AccountSnapshot(
        account_ref=account.account_ref,
        as_of=at,
        status=status,
        balances=({"asset": "USDT", "free": free, "locked": "0"},),
    )
    batch = TradeBatch(account_ref=account.account_ref, symbol="BTCUSDT")
    return await facts.ingest(
        batch,
        value,
        JournalEvent(
            event_id="refresh-account",
            aggregate_id=account.account_ref,
            kind="trades_imported",
            payload=batch,
            occurred_at=at,
        ),
    )


@pytest.mark.asyncio
async def test_account_change_after_read_is_superseded_and_same_balance_refresh_is_allowed(context):
    path, facts, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW + timedelta(seconds=1)))
    await store.claim(request(account))
    old_read = completion(account, NOW + timedelta(seconds=1))
    await refresh(facts, account, free="999")
    result = await store.finish("request-1", old_read)
    assert result.status == "superseded" and "account_changed" in result.reasons


@pytest.mark.asyncio
async def test_same_balance_refresh_does_not_invalidate_real_account_revision(context):
    path, facts, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW + timedelta(seconds=1)))
    await store.claim(request(account))
    old_read = completion(account, NOW + timedelta(seconds=1))
    refreshed = await refresh(facts, account)
    assert refreshed.account_revision == 1
    assert (await store.finish("request-1", old_read)).status == "published"


@pytest.mark.asyncio
async def test_account_failure_after_read_becomes_unavailable_without_candidate_publication(
    context,
):
    path, facts, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW + timedelta(seconds=1)))
    await store.claim(request(account))
    old_read = completion(account, NOW + timedelta(seconds=1))
    await refresh(facts, account, status="unavailable")
    result = await store.finish("request-1", old_read)
    assert result.status == "unavailable" and "account_unavailable" in result.reasons
    assert result.recommendation is None and await facts.load("advice-1") is None


@pytest.mark.asyncio
async def test_waiting_for_transaction_cannot_extend_risk_or_request_lifetime(context):
    path, _, _, _, account = context
    _, adapter = modules()
    clock = FakeClock(NOW)
    store = adapter.SqliteDecisionStore(path, clock=clock)
    await store.claim(request(account))
    old_read = completion(account)
    clock.advance_to(NOW + timedelta(seconds=6))
    result = await store.finish("request-1", old_read)
    assert result.status == "unavailable" and "publication_expired" in result.reasons


@pytest.mark.asyncio
async def test_time_expiry_during_audit_discards_candidate_in_same_transaction(
    context, monkeypatch
):
    path, facts, _, _, account = context
    _, adapter = modules()
    clock = FakeClock(NOW)
    store = adapter.SqliteDecisionStore(path, clock=clock)
    await store.claim(request(account))
    original_append = store._append

    def delayed(connection, event):
        receipt = original_append(connection, event)
        if event.kind == "state_changed" and event.payload.state.status == "published":
            clock.advance_to(NOW + timedelta(seconds=6))
        return receipt

    monkeypatch.setattr(store, "_append", delayed)
    result = await store.finish("request-1", completion(account))
    assert result.status == "unavailable" and "publication_expired" in result.reasons
    assert await facts.load("advice-1") is None
    page = await facts.scan(0, 100)
    assert not [item for item in page.records if item.event.aggregate_id == "advice-1"]


@pytest.mark.asyncio
async def test_final_audit_failure_rolls_back_advice_and_completion_but_keeps_claim(context):
    path, facts, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    before = len((await facts.scan(0, 100)).records)
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TRIGGER reject_finish BEFORE INSERT ON journal_events
            WHEN NEW.kind='decision_completed'
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    with pytest.raises(PersistenceUnavailable):
        await store.finish("request-1", completion(account))
    assert await facts.load("advice-1") is None
    assert (await store.decision("request-1")).result is None
    assert len((await facts.scan(0, 100)).records) == before


@pytest.mark.asyncio
async def test_unavailable_completion_is_persisted_without_inventing_advice(context):
    path, _, _, _, account = context
    domain, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    result = DecisionResult(
        request_id="request-1",
        snapshot_id="snapshot-1",
        status="unavailable",
        risk=dict(
            snapshot_id="snapshot-1",
            outcome="unavailable",
            evaluated_at=NOW,
            reasons=("provider_timeout",),
        ),
        reasons=("provider_timeout",),
    )
    value = domain.DecisionCompletion(result=result, completed_at=NOW)
    assert await store.finish("request-1", value) == result
    assert (await store.decision("request-1")).result == result


@pytest.mark.asyncio
async def test_completion_cannot_rewrite_input_style_evidence_or_immutable_result(context):
    path, _, _, _, account = context
    domain, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    good = completion(account)
    altered = good.model_dump()
    altered["result"]["recommendation"]["style_revision"] = 2
    with pytest.raises(ValueError):
        await store.finish("request-1", domain.DecisionCompletion(**altered))
    assert (await store.decision("request-1")).result is None
    await store.finish("request-1", good)
    altered = good.model_dump()
    altered["result"]["recommendation"]["assessment"]["explanation"] = "改写原始建议"
    with pytest.raises(RequestIdentityConflict):
        await store.finish("request-1", domain.DecisionCompletion(**altered))


@pytest.mark.asyncio
async def test_v6_migration_preserves_facts_and_creates_a_locked_backup(context):
    path, facts, sessions, running, account = context
    modules()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS decision_requests")
        connection.execute("PRAGMA user_version=6")
    await facts.initialize()
    assert await sessions.get("session-1") == running
    assert await facts.account_snapshot("local-spot") == account
    backups = tuple(path.parent.glob(path.name + f".pre-v{SCHEMA_VERSION}-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"route_id": "paid-route-B"},
        {"model_version": "model-B"},
        {"estimated_cost_usd": "0.02"},
        {"recorded_at": NOW + timedelta(seconds=1)},
        {"actual_cost_usd": "1e1000000"},
        {"actual_cost_usd": "1e-1000000"},
    ],
)
async def test_completion_cannot_bypass_claimed_fee_identity_and_reservation(context, change):
    path, facts, _, _, account = context
    domain, adapter = modules()
    value = request(account).model_dump()
    value["route"] = dict(
        route_id="paid-route-A",
        kind="economy",
        purpose="advisory",
        reason="fixture",
        model_version="model-A",
        price_version="price-A",
        paid=True,
    )
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(domain.DecisionRequest(**value))
    await facts.reserve(
        BudgetRequest(
            request_id="request-1",
            route_id="paid-route-A",
            purpose="advisory",
            price_version="price-A",
            estimated_cost_usd="0.01",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    usage = dict(
        request_id="request-1",
        route_id="paid-route-A",
        model_version="model-A",
        input_tokens=20,
        output_tokens=10,
        estimated_cost_usd="0.01",
        actual_cost_usd="0.01",
        billing_status="confirmed",
        recorded_at=NOW,
    )
    usage.update(change)
    altered = completion(account).model_dump()
    altered["result"]["usage"] = ModelUsage(**usage)
    with pytest.raises(ValueError):
        await store.finish("request-1", domain.DecisionCompletion(**altered))
    assert (await store.decision("request-1")).result is None


@pytest.mark.asyncio
async def test_v6_shadow_table_cannot_be_marked_as_successful_v7_migration(context):
    path, facts, _, _, _ = context
    modules()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE decision_requests")
        connection.execute("CREATE TABLE decision_requests(wrong_column TEXT)")
        connection.execute("PRAGMA user_version=6")
    with pytest.raises(PersistenceUnavailable):
        await facts.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
    assert tuple(path.parent.glob(path.name + f".pre-v{SCHEMA_VERSION}-*.bak"))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["final", "complete"])
async def test_preappended_audit_cannot_fake_completed_publication_or_reverse_stage_order(
    context, phase
):
    path, facts, _, _, account = context
    _, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    candidate = completion(account)
    identity = "decision:" + sha256(b"request-1").hexdigest() + ":" + phase
    if phase == "final":
        payload = StateRecord(
            key="advice-1",
            revision=2,
            state_type="recommendation",
            state=candidate.result.recommendation,
            updated_at=NOW,
        )
        audit = JournalEvent(
            event_id=identity,
            aggregate_id="advice-1",
            kind="state_changed",
            payload=payload,
            occurred_at=NOW,
        )
    else:
        audit = JournalEvent(
            event_id=identity,
            aggregate_id="request-1",
            kind="decision_completed",
            payload=candidate,
            occurred_at=NOW,
        )
    await facts.append(audit)
    with pytest.raises(EventIdentityConflict):
        await store.finish("request-1", candidate)
    assert await facts.load("advice-1") is None
    assert (await store.decision("request-1")).result is None


@pytest.mark.asyncio
async def test_reused_snapshot_identity_cannot_describe_different_frozen_facts(context):
    path, _, _, _, account = context
    domain, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    altered = request(account).model_dump()
    altered["request_id"] = "request-2"
    altered["snapshot"]["features"]["ema_fast"] = "61000"
    with pytest.raises(EventIdentityConflict):
        await store.claim(domain.DecisionRequest(**altered))
    assert await store.decision("request-2") is None


def test_publication_risk_cannot_predate_its_current_evidence():
    domain, _ = modules()
    account = AccountSnapshot(account_ref="local-spot", as_of=NOW)
    altered = completion(account, NOW + timedelta(seconds=1)).model_dump()
    altered["result"]["risk"]["evaluated_at"] = NOW
    with pytest.raises(ValueError):
        domain.DecisionCompletion(**altered)


@pytest.mark.asyncio
async def test_matching_settled_usage_is_part_of_the_immutable_completion(context):
    path, facts, _, _, account = context
    domain, adapter = modules()
    value = request(account).model_dump()
    value["route"] = dict(
        route_id="paid-route-A",
        kind="economy",
        purpose="advisory",
        reason="fixture",
        model_version="model-A",
        price_version="price-A",
        paid=True,
    )
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(domain.DecisionRequest(**value))
    reservation = await facts.reserve(
        BudgetRequest(
            request_id="request-1",
            route_id="paid-route-A",
            purpose="advisory",
            price_version="price-A",
            estimated_cost_usd="0.01",
            daily_limit_usd="1",
            requested_at=NOW,
        )
    )
    usage = ModelUsage(
        request_id="request-1",
        route_id="paid-route-A",
        model_version="model-A",
        input_tokens=20,
        output_tokens=10,
        estimated_cost_usd="0.01",
        actual_cost_usd="0.01",
        billing_status="confirmed",
        recorded_at=NOW,
    )
    await facts.settle(reservation.reservation_id, usage)
    candidate = completion(account).model_dump()
    candidate["result"]["usage"] = usage
    result = await store.finish("request-1", domain.DecisionCompletion(**candidate))
    assert result.status == "published" and result.usage.actual_cost_usd == usage.actual_cost_usd


@pytest.mark.asyncio
async def test_publication_snapshot_identity_is_also_immutable_across_requests(context):
    path, facts, _, _, account = context
    domain, adapter = modules()
    store = adapter.SqliteDecisionStore(path, clock=FakeClock(NOW))
    await store.claim(request(account))
    await store.finish("request-1", completion(account))
    another = request(account).model_dump()
    another["request_id"] = "request-2"
    await store.claim(domain.DecisionRequest(**another))
    altered = completion(account).model_dump()
    altered["result"]["request_id"] = "request-2"
    altered["result"]["recommendation"]["recommendation_id"] = "advice-2"
    altered["publication_snapshot"]["market"]["book"]["ask"] = "61000"
    with pytest.raises(EventIdentityConflict):
        await store.finish("request-2", domain.DecisionCompletion(**altered))
    assert await facts.load("advice-2") is None
    assert (await store.decision("request-2")).result is None
