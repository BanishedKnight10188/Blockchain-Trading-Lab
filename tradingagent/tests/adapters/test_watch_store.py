"""Atomic consumption, durable conflicts and isolated lanes."""

import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.domain.watch_rules import evaluate_watch
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.fixtures.watch_cases import NOW, definition, frame


async def stores(path):
    watches = importlib.import_module("agent_platform.adapters.sqlite.watches").SqliteWatchStore(
        path
    )
    events = importlib.import_module(
        "agent_platform.adapters.sqlite.agent_events"
    ).SqliteAgentEventStore(path)
    await watches.initialize()
    return watches, events


@pytest.mark.asyncio
async def test_trigger_commit_and_outbox_are_atomic(tmp_path):
    path = tmp_path / "core.sqlite"
    store, events = await stores(path)
    original = await store.create(definition())
    snapshot = frame()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER abort_outbox BEFORE INSERT ON agent_event_deliveries "
            "BEGIN SELECT RAISE(ABORT,'fault'); END"
        )
    with pytest.raises(PersistenceUnavailable):
        await store.commit_evaluation(
            "watch-1", 1, snapshot, evaluate_watch(original, snapshot, NOW)
        )
    assert await store.get("watch-1") == original
    assert await events.claim("lane-1", NOW, 120) is None
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER abort_outbox")
    committed = await store.commit_evaluation(
        "watch-1", 1, snapshot, evaluate_watch(original, snapshot, NOW)
    )
    _, reopened = await stores(path)
    assert (await reopened.claim("lane-1", NOW, 120)).event.event_id == committed.event_id


@pytest.mark.asyncio
async def test_duplicate_candle_one_event_and_cas(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    original = await store.create(definition())
    result = evaluate_watch(original, frame(), NOW)
    committed = await store.commit_evaluation("watch-1", 1, frame(), result)
    with pytest.raises(RevisionConflict):
        await store.commit_evaluation("watch-1", 1, frame(), result)
    again = await store.commit_evaluation(
        "watch-1", committed.revision, frame(), evaluate_watch(committed, frame(), NOW)
    )
    assert again == committed
    assert await events.claim("lane-2", NOW, 120) is None
    assert await events.claim("lane-1", NOW, 120) is not None
    assert await events.claim("lane-1", NOW, 120) is None


@pytest.mark.asyncio
async def test_watch_revision_capacity_replace_and_cancel(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    for i in range(20):
        await store.create(definition(watch_id=f"w-{i}"))
    with pytest.raises(ValueError, match="capacity"):
        await store.create(definition(watch_id="w-20"))
    with pytest.raises(RevisionConflict):
        await store.cancel("w-0", 9, NOW)
    cancelled = await store.cancel("w-0", 1, NOW)
    assert cancelled.state == "CANCELLED"
    await store.create(definition(watch_id="w-20"))
    await store.create(definition(watch_id="other", lane_id="lane-2"))
    with pytest.raises(ValueError):
        await store.replace(definition(watch_id="w-0", definition_revision=2), cancelled.revision)
    replacement = await store.replace(definition(watch_id="w-1", definition_revision=2), 1)
    assert replacement.definition.version == 2 and replacement.revision == 2
    with pytest.raises(ValueError):
        await store.replace(definition(watch_id="w-1", definition_revision=3, lane_id="lane-2"), 2)
    assert len(await store.list_active("lane-1")) == 20


@pytest.mark.asyncio
async def test_conflict_is_durable_and_cannot_invalidate_or_trigger(tmp_path):
    path = tmp_path / "core.sqlite"
    store, _ = await stores(path)
    first = await store.create(
        definition(
            trigger={
                "logic": "ALL",
                "conditions": [{"metric": "candle.close", "op": "GT", "value": "109"}],
            }
        )
    )
    first = await store.commit_evaluation(
        "watch-1", 1, frame(), evaluate_watch(first, frame(), NOW)
    )
    changed = frame(tuple(c.model_copy(update={"volume": c.volume + 1}) for c in frame().candles))
    conflict = await store.commit_evaluation(
        "watch-1", first.revision, changed, evaluate_watch(first, changed, NOW)
    )
    assert conflict.state == "ARMED" and conflict.reason == "data_conflict"
    reopened, _ = await stores(path)
    fresh = await reopened.create(definition(watch_id="new"))
    blocked = await reopened.commit_evaluation(
        "new", 1, frame(), evaluate_watch(fresh, frame(), NOW)
    )
    assert blocked.state == "ARMED" and blocked.reason == "data_conflict"


@pytest.mark.asyncio
async def test_late_trigger_archived_and_expiry_without_market(tmp_path):
    store, events = await stores(tmp_path / "core.sqlite")
    original = await store.create(definition())
    late = NOW + timedelta(seconds=121)
    snapshot = frame(received_at=late)
    committed = await store.commit_evaluation(
        "watch-1", 1, snapshot, evaluate_watch(original, snapshot, late)
    )
    assert committed.state == "TRIGGERED"
    assert await events.claim("lane-1", late, 120) is None
    assert (await events.delivery(committed.event_id)).status == "SUPPRESSED"
    await store.create(definition(watch_id="expired", expires_at=NOW))
    assert (await store.expire("lane-1", NOW))[0].state == "EXPIRED"


@pytest.mark.asyncio
async def test_v7_migration_and_watch_backup(tmp_path):
    from agent_platform.adapters.sqlite import schema
    from agent_platform.adapters.sqlite.backup import backup_database

    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        for name in (
            "SESSION_SCHEMA",
            "JOURNAL_SCHEMA",
            "BUDGET_SCHEMA",
            "STATE_SCHEMA",
            "OBSERVATION_SCHEMA",
            "QUOTE_EVIDENCE_SCHEMA",
            "DECISION_SCHEMA",
        ):
            for statement in getattr(schema, name):
                db.execute(statement)
        db.execute("PRAGMA user_version=7")
        db.execute(
            "INSERT INTO journal_events(event_id,aggregate_id,kind,occurred_at,body) "
            "VALUES('existing','jev','fact','2026-10-09','preserve verbatim')"
        )
    old_copy = await backup_database(path, tmp_path / "v7.bak", kind="core")
    assert old_copy["source_schema_version"] == 7
    store, _ = await stores(path)
    original = await store.create(definition())
    await store.commit_evaluation("watch-1", 1, frame(), evaluate_watch(original, frame(), NOW))
    report = await backup_database(path, tmp_path / "v8.bak", kind="core")
    assert report["source_schema_version"] == schema.SCHEMA_VERSION
    assert list(tmp_path.glob(f"legacy.sqlite.pre-v{schema.SCHEMA_VERSION}-*.bak"))
    restored, events = await stores(tmp_path / "v8.bak")
    assert (await restored.get("watch-1")).state == "TRIGGERED"
    assert await events.claim("lane-1", NOW, 120)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT body FROM journal_events").fetchone()[0] == "preserve verbatim"


@pytest.mark.asyncio
async def test_numeric_representation_is_not_a_market_conflict(tmp_path):
    from decimal import Decimal

    store, _ = await stores(tmp_path / "core.sqlite")
    watch = await store.create(
        definition(
            trigger={
                "logic": "ALL",
                "conditions": [
                    {"metric": "candle.close", "op": "GT", "value": "109"},
                ],
            }
        )
    )
    original = frame()
    watch = await store.commit_evaluation(
        "watch-1", 1, original, evaluate_watch(watch, original, NOW)
    )
    scaled = frame(
        tuple(
            c.model_copy(
                update={
                    name: Decimal(str(getattr(c, name)) + ".00")
                    for name in ("open", "high", "low", "close", "volume")
                }
            )
            for c in original.candles
        )
    )
    assert scaled.candles == original.candles
    assert scaled.content_hash == original.content_hash
    result = await store.commit_evaluation(
        "watch-1", watch.revision, scaled, evaluate_watch(watch, scaled, NOW)
    )
    assert result.reason == "conditions_false" and result == watch
