"""The assembly exposes all four persistence ports and safely upgrades old data."""

import importlib
import sqlite3

import pytest

from agent_platform.domain.events import SessionJournalEvent, StateRecord
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.persistence import (
    BudgetStorePort,
    EventStorePort,
    ObservationStorePort,
    StateStorePort,
)
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.adapters.test_sqlite_budgets import request, usage
from tests.adapters.test_sqlite_state import event
from tests.domain.test_decisions import NOW


@pytest.mark.asyncio
async def test_one_factory_exposes_all_four_ports(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    store = await module.open_store(tmp_path / "agent.sqlite3")
    for port in (EventStorePort, StateStorePort, ObservationStorePort, BudgetStorePort):
        assert isinstance(store, port)
    assert await store.cursor("offline-spot", "BTCUSDT") is not None
    reservation = await store.reserve(request())
    assert (
        await store.settle(reservation.reservation_id, usage(reservation, "0.4"))
    ).spent_usd == usage(reservation, "0.4").actual_cost_usd


@pytest.mark.asyncio
async def test_version_six_quote_foreign_key_must_defer_audit_reference(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    path = tmp_path / "wrong-v6.sqlite3"
    await module.open_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE trade_quote_evidence")
        connection.execute(
            "CREATE TABLE trade_quote_evidence ("
            "trade_sequence INTEGER PRIMARY KEY REFERENCES observed_trades(sequence), "
            "quote_quantity TEXT NOT NULL, "
            "source_event_id TEXT NOT NULL REFERENCES journal_events(event_id))"
        )
    with pytest.raises(PersistenceUnavailable):
        await module.open_store(path)


@pytest.mark.asyncio
async def test_version_three_upgrade_preserves_session_budget_and_unknown_usage(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    schemas = importlib.import_module("agent_platform.adapters.sqlite.schema")
    budgets = importlib.import_module("agent_platform.adapters.sqlite.budgets")
    sessions = importlib.import_module("agent_platform.adapters.sqlite.sessions")
    path = tmp_path / "old-v3.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in (*schemas.SESSION_SCHEMA, *schemas.JOURNAL_SCHEMA, *schemas.BUDGET_SCHEMA):
            connection.execute(statement)
        connection.execute("PRAGMA user_version=3")
    old_budget = budgets.SqliteBudgetStore(path)
    original_request = request()
    reservation = await old_budget.reserve(original_request)
    await old_budget.settle(reservation.reservation_id, usage(reservation))
    old_session = sessions.SqliteSessionStore(path)
    original = AgentSession(
        session_id="session-1", style={"strength": 67}, created_at=NOW, updated_at=NOW
    )
    await old_session.create(
        original,
        SessionJournalEvent(
            event_id="created-1", kind="created", session=original, occurred_at=NOW
        ),
    )
    upgraded = await module.open_store(path)
    assert (await upgraded.load(original.session_id)).state == original
    assert (await upgraded.reserve(original_request)).status == "unknown"
    backups = tuple(tmp_path.glob("old-v3.sqlite3.pre-v*-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert backup.execute("SELECT COUNT(*) FROM budget_requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_backup_is_copied_while_migration_excludes_other_writers(tmp_path, monkeypatch):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    migrations = importlib.import_module("agent_platform.adapters.sqlite.migrations")
    schemas = importlib.import_module("agent_platform.adapters.sqlite.schema")
    path = tmp_path / "locked-migration.sqlite3"
    with sqlite3.connect(path) as connection:
        for statement in schemas.SESSION_SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
    original_backup = migrations._backup_database
    checked = []

    def guarded_backup(database):
        with sqlite3.connect(database, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("BEGIN IMMEDIATE")
        checked.append(True)
        original_backup(database)

    monkeypatch.setattr(migrations, "_backup_database", guarded_backup)
    await module.open_store(path)
    assert checked == [True]
    with sqlite3.connect(next(tmp_path.glob("*.bak"))) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_incompatible_legacy_columns_are_rejected_without_marking_upgrade(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    path = tmp_path / "unknown-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE sessions(session_id TEXT PRIMARY KEY, revision INTEGER, status TEXT,
                active_slot INTEGER NOT NULL DEFAULT 1);
            PRAGMA user_version=1;
        """)
    with pytest.raises(PersistenceUnavailable):
        await module.open_store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["primary_key", "unique", "foreign_key", "active_index"])
async def test_same_columns_with_missing_constraints_are_not_upgraded(tmp_path, defect):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    path = tmp_path / "unknown-constraints.sqlite3"
    primary_key = "" if defect == "primary_key" else "PRIMARY KEY"
    unique = "" if defect == "unique" else "UNIQUE"
    reference = "" if defect == "foreign_key" else "REFERENCES sessions(session_id)"
    with sqlite3.connect(path) as connection:
        connection.executescript(f"""
            CREATE TABLE sessions(session_id TEXT {primary_key}, revision INTEGER NOT NULL,
                status TEXT NOT NULL, active_slot INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL);
            CREATE TABLE session_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL {unique}, session_id TEXT NOT NULL {reference},
                revision INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(session_id, revision));
            PRAGMA user_version=1;
        """)
        if defect != "active_index":
            connection.execute(
                "CREATE UNIQUE INDEX one_open_session ON sessions(active_slot) "
                "WHERE status <> 'closed'"
            )
    with pytest.raises(PersistenceUnavailable):
        await module.open_store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not tuple(tmp_path.glob("*.bak"))


@pytest.mark.asyncio
async def test_legacy_style_save_cannot_reopen_a_closed_session(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite")
    sessions = importlib.import_module("agent_platform.adapters.sqlite.sessions")
    store = await module.open_store(tmp_path / "agent.sqlite3")
    legacy = sessions.SqliteSessionStore(store.path)
    original = AgentSession(
        session_id="session-1", style={"strength": 67}, created_at=NOW, updated_at=NOW
    )
    await legacy.create(
        original,
        SessionJournalEvent(
            event_id="created-1", kind="created", session=original, occurred_at=NOW
        ),
    )
    closed = original.transition("closed", NOW)
    closed_record = StateRecord(
        key=closed.session_id, revision=2, state_type="session", state=closed, updated_at=NOW
    )
    await store.save(closed_record, 1, event(closed_record, "closed-1"))
    forged = AgentSession.model_validate(
        {
            **closed.model_dump(),
            "status": "running",
            "revision": 3,
            "style": {"strength": 72},
            "style_revision": 2,
        }
    )
    with pytest.raises(ValueError):
        await legacy.save(
            forged,
            2,
            SessionJournalEvent(
                event_id="forged-1", kind="style_changed", session=forged, occurred_at=NOW
            ),
        )
    assert await legacy.active() is None
