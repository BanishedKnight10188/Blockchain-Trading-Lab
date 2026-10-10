"""A durable ordered journal is idempotent and compatible with existing sessions."""

import importlib
import sqlite3
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from agent_platform.domain.events import JournalEvent, SessionJournalEvent
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.persistence import EventIdentityConflict, EventStorePort
from agent_platform.ports.sessions import PersistenceUnavailable

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def event(event_id="event-1", strength=67):
    session = AgentSession(
        session_id="session-1", style={"strength": strength}, created_at=NOW, updated_at=NOW
    )
    return JournalEvent(
        event_id=event_id,
        aggregate_id="session-1",
        kind="session_changed",
        payload=session,
        occurred_at=NOW,
    )


@pytest_asyncio.fixture
async def context(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite.events")
    path = tmp_path / "agent.sqlite3"
    return module, await module.open_store(path), path


@pytest.mark.asyncio
async def test_duplicate_event_id_is_idempotent_but_different_content_is_rejected(context):
    _, store, _ = context
    assert isinstance(store, EventStorePort)
    first = await store.append(event())
    repeat = await store.append(event())
    assert first.appended
    assert not repeat.appended
    assert first.sequence == repeat.sequence
    with pytest.raises(EventIdentityConflict):
        await store.append(event(strength=68))
    assert len((await store.scan(0, 10)).records) == 1


@pytest.mark.asyncio
async def test_pages_resume_in_order_and_empty_page_keeps_the_cursor(context):
    _, store, _ = context
    receipts = [await store.append(event(f"event-{index}")) for index in range(3)]
    first = await store.scan(0, 2)
    second = await store.scan(first.next_sequence, 2)
    empty = await store.scan(second.next_sequence, 2)
    assert [item.sequence for item in first.records + second.records] == [
        item.sequence for item in receipts
    ]
    assert not empty.records
    assert empty.next_sequence == second.next_sequence
    for cursor, limit in ((-1, 10), (0, 0), (True, 10), (0, 1001)):
        with pytest.raises(ValueError):
            await store.scan(cursor, limit)


@pytest.mark.asyncio
async def test_reopening_keeps_events_and_sequence(context):
    module, store, path = context
    first = await store.append(event())
    reopened = await module.open_store(path)
    page = await reopened.scan(0, 10)
    assert page.records[0].event == event()
    assert (await reopened.append(event("event-2"))).sequence > first.sequence


@pytest.mark.asyncio
async def test_version_one_migration_backs_up_and_preserves_session(tmp_path):
    sessions = importlib.import_module("agent_platform.adapters.sqlite.sessions")
    module = importlib.import_module("agent_platform.adapters.sqlite.events")
    path = tmp_path / "old.sqlite3"
    # Version-one schema is an actual previous database, not whatever initialize creates today.
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE sessions(session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                status TEXT NOT NULL, active_slot INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL);
            CREATE UNIQUE INDEX one_open_session ON sessions(active_slot) WHERE status <> 'closed';
            CREATE TABLE session_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                revision INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(session_id, revision));
            PRAGMA user_version = 1;
        """)
    original = event().payload
    original_store = sessions.SqliteSessionStore(path)
    await original_store.create(
        original,
        SessionJournalEvent(
            event_id="created-1", kind="created", session=original, occurred_at=NOW
        ),
    )
    store = await module.open_store(path)
    backups = tuple(tmp_path.glob("old.sqlite3.pre-v*-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    await original_store.initialize()
    assert await original_store.active() == original
    assert len(await original_store.history("session-1")) == 1
    changed = original.change_style(type(original.style)(strength=100), NOW)
    await original_store.save(
        changed,
        1,
        SessionJournalEvent(
            event_id="changed-1", kind="style_changed", session=changed, occurred_at=NOW
        ),
    )
    assert await original_store.active() == changed
    assert (await store.append(event())).appended


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["unsupported_version", "corrupt"])
async def test_bad_database_is_rejected_with_a_generic_error(tmp_path, broken):
    module = importlib.import_module("agent_platform.adapters.sqlite.events")
    path = tmp_path / "bad.sqlite3"
    if broken == "corrupt":
        path.write_bytes(b"not-a-database-with-api_secret=never-display")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version = 999")
    with pytest.raises(PersistenceUnavailable) as failure:
        await module.open_store(path)
    assert "api_secret" not in str(failure.value)
