"""CAS projections and audit commit together; sessions retain one authoritative table."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest
import pytest_asyncio

from agent_platform.domain.decisions import Recommendation
from agent_platform.domain.events import JournalEvent, SessionJournalEvent, StateRecord
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.persistence import EventIdentityConflict, StateStorePort
from agent_platform.ports.sessions import (
    ActiveSessionExists,
    PersistenceUnavailable,
    RevisionConflict,
)
from tests.domain.test_decisions import NOW, recommendation_data


@pytest_asyncio.fixture
async def context(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite.state")
    path = tmp_path / "agent.sqlite3"
    store = module.SqliteStateStore(path)
    await store.initialize()
    return module, store, path


def record(advice=None, revision=1):
    advice = advice or Recommendation(**recommendation_data())
    return StateRecord(
        key=advice.recommendation_id,
        revision=revision,
        state_type="recommendation",
        state=advice,
        updated_at=advice.updated_at,
    )


def event(record, event_id="state-event-1"):
    return JournalEvent(
        event_id=event_id,
        aggregate_id=record.key,
        kind="state_changed",
        payload=record,
        occurred_at=record.updated_at,
    )


@pytest.mark.asyncio
async def test_cas_creation_update_and_reopen(context):
    module, store, path = context
    original = record()
    assert isinstance(store, StateStorePort)
    assert await store.load(original.key) is None
    assert await store.save(original, 0, event(original)) == original
    published = original.state.transition("published", NOW + timedelta(seconds=1))
    changed = record(published, 2)
    await store.save(changed, 1, event(changed, "state-event-2"))
    reopened = module.SqliteStateStore(path)
    await reopened.initialize()
    assert await reopened.load(original.key) == changed
    assert len((await reopened.scan(0, 10)).records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["published", "accepted", "rejected", "expired"])
async def test_initial_recommendation_cannot_skip_created_audit(context, status):
    _, store, _ = context
    data = recommendation_data()
    data["status"] = status
    if status == "expired":
        data["updated_at"] = data["expires_at"]
    initial = record(Recommendation(**data))
    with pytest.raises(ValueError):
        await store.save(initial, 0, event(initial))
    assert await store.load(initial.key) is None
    assert not (await store.scan(0, 10)).records


@pytest.mark.asyncio
async def test_two_independent_writers_cannot_replace_the_same_revision(context):
    module, store, path = context
    original = record()
    await store.save(original, 0, event(original))
    second = module.SqliteStateStore(path)
    await second.initialize()
    candidates = [
        record(original.state.transition(status, NOW + timedelta(seconds=1)), 2)
        for status in ("published", "superseded")
    ]
    results = await asyncio.gather(
        store.save(candidates[0], 1, event(candidates[0], "writer-1")),
        second.save(candidates[1], 1, event(candidates[1], "writer-2")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert (await store.load(original.key)).revision == 2
    assert len((await store.scan(0, 10)).records) == 2


@pytest.mark.asyncio
async def test_retry_of_committed_event_never_overwrites_newer_state(context):
    _, store, _ = context
    original = record()
    first_event = event(original)
    await store.save(original, 0, first_event)
    changed = record(original.state.transition("published", NOW + timedelta(seconds=1)), 2)
    await store.save(changed, 1, event(changed, "state-event-2"))
    assert await store.save(original, 0, first_event) == original
    assert await store.load(original.key) == changed
    with pytest.raises(EventIdentityConflict):
        await store.save(changed, 1, event(changed, first_event.event_id))


@pytest.mark.asyncio
async def test_appending_an_event_alone_cannot_fake_a_committed_state_save(context):
    _, store, _ = context
    original = record()
    await store.append(event(original))
    assert await store.load(original.key) is None
    await store.save(original, 0, event(original))
    assert await store.load(original.key) == original


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_projection(context):
    _, store, path = context
    original = record()
    await store.save(original, 0, event(original))
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_state_audit BEFORE INSERT ON journal_events
            BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END;
        """)
    changed = record(original.state.transition("published", NOW + timedelta(seconds=1)), 2)
    with pytest.raises(PersistenceUnavailable):
        await store.save(changed, 1, event(changed, "state-event-2"))
    assert await store.load(original.key) == original


@pytest.mark.asyncio
async def test_mismatched_audit_and_forged_original_advice_are_rejected(context):
    _, store, _ = context
    original = record()
    await store.save(original, 0, event(original))
    changed = record(original.state.transition("published", NOW + timedelta(seconds=1)), 2)
    with pytest.raises(ValueError):
        await store.save(changed, 1, event(original, "wrong-event"))
    forged = Recommendation.model_validate(
        {
            **changed.state.model_dump(),
            "assessment": {
                "action": "buy",
                "source": "fake",
                "explanation": "修改原始建议",
                "evidence_ids": ("fake-evidence",),
            },
        }
    )
    forged_record = record(forged, 2)
    with pytest.raises(ValueError):
        await store.save(forged_record, 1, event(forged_record, "forged-event"))
    assert await store.load(original.key) == original


@pytest.mark.asyncio
async def test_generic_session_updates_share_the_existing_session_authority(context):
    _, store, path = context
    sessions_module = importlib.import_module("agent_platform.adapters.sqlite.sessions")
    legacy = sessions_module.SqliteSessionStore(path)
    original = AgentSession(
        session_id="session-1", style={"strength": 67}, created_at=NOW, updated_at=NOW
    )
    await legacy.create(
        original,
        SessionJournalEvent(
            event_id="created-1", kind="created", session=original, occurred_at=NOW
        ),
    )
    running = original.transition("running", NOW + timedelta(seconds=1))
    running_record = StateRecord(
        key=original.session_id,
        revision=2,
        state_type="session",
        state=running,
        updated_at=running.updated_at,
    )
    await store.save(running_record, 1, event(running_record))
    assert await legacy.active() == running
    assert (await store.load(original.session_id)).state == running
    changed = running.change_style(type(running.style)(strength=72), NOW + timedelta(seconds=2))
    await legacy.save(
        changed,
        2,
        SessionJournalEvent(
            event_id="style-1",
            kind="style_changed",
            session=changed,
            occurred_at=changed.updated_at,
        ),
    )
    assert (await store.load(original.session_id)).state == changed
    other = AgentSession(
        session_id="session-2", style={"strength": 1}, created_at=NOW, updated_at=NOW
    )
    other_record = StateRecord(
        key=other.session_id, revision=1, state_type="session", state=other, updated_at=NOW
    )
    with pytest.raises(ActiveSessionExists):
        await store.save(other_record, 0, event(other_record, "other-session"))
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM domain_states WHERE state_type='session'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [-1, True, "0"])
async def test_revision_input_is_strict(context, expected):
    _, store, _ = context
    original = record()
    with pytest.raises(ValueError):
        await store.save(original, expected, event(original))
