"""Session persistence must keep style revisions and audit facts together."""

import asyncio
import importlib
import sqlite3
from datetime import UTC, datetime

import pytest
import pytest_asyncio


class FixedClock:
    def utcnow(self):
        return datetime(2026, 10, 5, tzinfo=UTC)

    def monotonic(self):
        return 0.0


@pytest_asyncio.fixture
async def context(tmp_path):
    adapter = importlib.import_module("agent_platform.adapters.sqlite.sessions")
    application = importlib.import_module("agent_platform.application.sessions")
    domain = importlib.import_module("agent_platform.domain.sessions")
    ports = importlib.import_module("agent_platform.ports.sessions")
    path = tmp_path / "sessions.sqlite3"
    store = adapter.SqliteSessionStore(path)
    await store.initialize()
    service = application.SessionService(store, FixedClock())
    return adapter, service, store, domain, ports, path


@pytest.mark.asyncio
async def test_create_requires_explicit_style_and_one_open_session(context):
    _, service, store, domain, ports, _ = context
    with pytest.raises(TypeError):
        await service.create()
    original = await service.create(domain.TradingStyle(strength=28))
    assert original.style.strength == 28
    assert await store.active() == original
    assert isinstance(store, ports.SessionStorePort)
    with pytest.raises(ports.ActiveSessionExists):
        await service.create(domain.TradingStyle(strength=79))
    assert len(await store.history(original.session_id)) == 1


@pytest.mark.asyncio
async def test_style_change_survives_reopening_database(context):
    adapter, service, store, domain, _, path = context
    original = await service.create(domain.TradingStyle(strength=0))
    changed = await service.change_style(
        original.session_id,
        domain.TradingStyle(strength=100),
        expected_revision=1,
    )
    reopened = adapter.SqliteSessionStore(path)
    await reopened.initialize()
    assert await reopened.active() == changed
    history = await store.history(original.session_id)
    assert [event.session.style.strength for event in history] == [0, 100]
    assert [event.session.style_revision for event in history] == [1, 2]
    assert not changed.matches_style_revision(1)


@pytest.mark.asyncio
async def test_two_writers_cannot_overwrite_the_same_revision(context):
    _, service, store, domain, ports, _ = context
    original = await service.create(domain.TradingStyle(strength=50))
    outcomes = await asyncio.gather(
        service.change_style(original.session_id, domain.TradingStyle(strength=35), 1),
        service.change_style(original.session_id, domain.TradingStyle(strength=75), 1),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ports.RevisionConflict) for item in outcomes) == 1
    assert (await store.active()).revision == 2
    assert len(await store.history(original.session_id)) == 2


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_session_update(context):
    _, service, store, domain, ports, path = context
    original = await service.create(domain.TradingStyle(strength=25))
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_audit BEFORE INSERT ON session_events
            BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END;
        """)
    with pytest.raises(ports.PersistenceUnavailable):
        await service.change_style(original.session_id, domain.TradingStyle(strength=90), 1)
    assert await store.active() == original
    assert len(await store.history(original.session_id)) == 1


@pytest.mark.asyncio
async def test_same_strength_is_idempotent_but_stale_revision_is_rejected(context):
    _, service, store, domain, ports, _ = context
    original = await service.create(domain.TradingStyle(strength=50))
    unchanged = await service.change_style(original.session_id, original.style, 1)
    assert unchanged == original
    assert len(await store.history(original.session_id)) == 1
    changed = await service.change_style(original.session_id, domain.TradingStyle(strength=51), 1)
    with pytest.raises(ports.RevisionConflict):
        await service.change_style(original.session_id, changed.style, 1)


@pytest.mark.asyncio
async def test_unknown_session_returns_a_domain_error(context):
    _, service, _, domain, ports, _ = context
    with pytest.raises(ports.SessionNotFound):
        await service.change_style("missing", domain.TradingStyle(strength=50), 1)


@pytest.mark.asyncio
async def test_lifecycle_is_cas_audited_and_closed_session_releases_slot(context):
    _, service, store, domain, ports, _ = context
    original = await service.create(domain.TradingStyle(strength=39))
    running = await service.transition(original.session_id, "running", 1)
    assert running.revision == 2 and running.style_revision == 1
    with pytest.raises(ports.RevisionConflict):
        await service.transition(original.session_id, "paused", 1)
    paused = await service.transition(original.session_id, "paused", 2)
    assert paused.status == "paused" and paused.revision == 3
    assert await service.transition(original.session_id, "paused", 3) == paused
    closed = await service.transition(original.session_id, "closed", 3)
    assert closed.status == "closed" and await store.active() is None
    assert [event.kind for event in await store.history(original.session_id)] == [
        "created",
        "state_changed",
        "state_changed",
        "state_changed",
    ]
    with pytest.raises(ValueError):
        await service.transition(original.session_id, "running", 4)
    assert (await service.create(domain.TradingStyle(strength=40))).revision == 1


@pytest.mark.asyncio
async def test_lifecycle_audit_failure_and_invalid_revision_cannot_change_state(context):
    _, service, store, domain, ports, path = context
    original = await service.create(domain.TradingStyle(strength=39))
    with pytest.raises(ValueError):
        await service.transition(original.session_id, "running", True)
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_transition BEFORE INSERT ON session_events
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END;
        """)
    with pytest.raises(ports.PersistenceUnavailable):
        await service.transition(original.session_id, "running", 1)
    assert await store.active() == original
