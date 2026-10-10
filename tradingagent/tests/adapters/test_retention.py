"""Auxiliary market retention never touches permanent trading evidence."""

import asyncio
import importlib
import sqlite3
import threading
from datetime import timedelta

import pytest

from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.schema import SCHEMA_VERSION
from agent_platform.ports.persistence import ObservationConflict
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.domain.test_decisions import NOW


def record(at=NOW, *, kind="raw", price="60000.123456789", collected_at=None):
    model = importlib.import_module("agent_platform.domain.market_archive").MarketArchiveRecord
    payload = dict(
        kind="sample", price=price, quote_at=at, book_at=None, bid=None, ask=None, status="ready"
    )
    if kind == "minute":
        payload = dict(
            kind="candle",
            symbol="BTCUSDT",
            opened_at=at - timedelta(minutes=1),
            closed_at=at,
            open=price,
            high=price,
            low=price,
            close=price,
            volume="1",
            quote_volume="60000",
        )
    return model(
        kind=kind,
        mode="fake",
        source="fake",
        event_at=at,
        collected_at=collected_at or at,
        payload=payload,
    )


async def store(path):
    cls = importlib.import_module("agent_platform.adapters.sqlite.retention").SqliteMarketArchive
    value = cls(path)
    await value.initialize()
    return value


@pytest.mark.asyncio
async def test_core_database_created_between_archive_open_and_lock_is_never_overwritten(tmp_path):
    cls = importlib.import_module("agent_platform.adapters.sqlite.retention").SqliteMarketArchive
    path = tmp_path / "race.sqlite3"
    archive = cls(path)
    started, release = threading.Event(), threading.Event()

    class GatedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                started.set()
                if not release.wait(2):
                    raise RuntimeError("test gate expired")
            return super().execute(sql, *args)

    def connect():
        value = sqlite3.connect(path, factory=GatedConnection)
        value.row_factory = sqlite3.Row
        return value

    archive._connect = connect
    task = asyncio.create_task(archive.initialize())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await open_store(path)
        release.set()
        with pytest.raises(ValueError, match="separate"):
            await task
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='market_archive'"
                ).fetchone()
                is None
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pin_default", "pin_check"])
async def test_restored_schema_with_changed_pin_semantics_is_rejected(tmp_path, change):
    path = tmp_path / "restore.sqlite3"
    await store(path)
    with sqlite3.connect(path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='market_archive'"
        ).fetchone()[0]
        if change == "pin_default":
            sql = sql.replace("DEFAULT 0", "DEFAULT 1")
        else:
            sql = sql.replace("CHECK(pinned IN (0,1))", "")
        connection.execute("DROP TABLE market_archive")
        connection.execute(sql)
    with pytest.raises(ValueError, match="schema"):
        await store(path)


@pytest.mark.asyncio
async def test_exact_record_hash_and_retry_survive_reopen_without_rewriting_collection_time(
    tmp_path,
):
    value = await store(tmp_path / "market.sqlite3")
    first = record()
    assert await value.append_many((first,)) == (True,)
    retry = first.model_copy(update={"collected_at": NOW + timedelta(seconds=1)})
    assert retry.source_hash == first.source_hash
    assert await value.append_many((retry,)) == (False,)
    reopened = await store(value.path)
    assert await reopened.get(first.record_id) == first
    assert (
        await reopened.get(first.record_id)
    ).payload.price.as_tuple() == first.payload.price.as_tuple()


@pytest.mark.asyncio
async def test_conflicting_same_identity_rolls_back_whole_batch(tmp_path):
    value = await store(tmp_path / "market.sqlite3")
    first = record()
    assert await value.append_many((first,)) == (True,)
    second = record(NOW + timedelta(seconds=1))
    with pytest.raises(ObservationConflict):
        await value.append_many((second, record(price="61000")))
    assert await value.get(first.record_id) == first
    assert await value.get(second.record_id) is None


@pytest.mark.asyncio
async def test_seven_ninety_day_microsecond_boundaries_and_pinned_evidence(tmp_path):
    value = await store(tmp_path / "market.sqlite3")
    raw_boundary = record(NOW - timedelta(days=7))
    raw_expired = record(raw_boundary.event_at - timedelta(microseconds=1))
    minute_boundary = record(NOW - timedelta(days=90), kind="minute")
    minute_expired = record(minute_boundary.event_at - timedelta(microseconds=1), kind="minute")
    pinned = record(NOW - timedelta(days=100))
    await value.append_many((raw_boundary, raw_expired, minute_boundary, minute_expired, pinned))
    await value.pin(pinned.record_id, pinned.source_hash)
    result = await value.prune(NOW)
    assert result.raw_removed == result.minute_removed == 1 and not result.has_more
    for item in (raw_boundary, minute_boundary, pinned):
        assert await value.get(item.record_id) == item
    assert await value.get(raw_expired.record_id) is None
    assert await value.get(minute_expired.record_id) is None


@pytest.mark.asyncio
async def test_prune_has_bounded_batch_and_reports_remaining(tmp_path):
    value = await store(tmp_path / "market.sqlite3")
    records = tuple(record(NOW - timedelta(days=8, seconds=i)) for i in range(3))
    await value.append_many(records)
    assert (await value.prune(NOW, limit=1)).has_more
    assert (await value.prune(NOW, limit=1)).has_more
    assert not (await value.prune(NOW, limit=1)).has_more
    for limit in (0, True, 1001):
        with pytest.raises(ValueError):
            await value.prune(NOW, limit=limit)


@pytest.mark.asyncio
async def test_main_database_refused_and_journal_untouched(tmp_path):
    path = tmp_path / "main.sqlite3"
    await open_store(path)
    with pytest.raises(ValueError):
        await store(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM journal_events").fetchone()[0] == 0
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='market_archive'"
        ).fetchall()


@pytest.mark.asyncio
async def test_corrupt_hash_is_not_read_as_valid_evidence(tmp_path):
    value = await store(tmp_path / "market.sqlite3")
    first = record()
    await value.append_many((first,))
    with sqlite3.connect(value.path) as connection:
        connection.execute("UPDATE market_archive SET source_hash=?", ("0" * 64,))
    with pytest.raises(PersistenceUnavailable):
        await value.get(first.record_id)


@pytest.mark.asyncio
async def test_online_backup_preserves_rows_pin_and_existing_target(tmp_path):
    value = await store(tmp_path / "market.sqlite3")
    first = record(NOW - timedelta(days=10))
    await value.append_many((first,))
    await value.pin(first.record_id, first.source_hash)
    destination = tmp_path / "backup.sqlite3"
    await value.backup(destination)
    recovered = await store(destination)
    assert await recovered.get(first.record_id) == first
    assert (await recovered.prune(NOW)).raw_removed == 0
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        await value.backup(destination)
    assert destination.read_bytes() == before


@pytest.mark.asyncio
async def test_write_error_is_fixed_and_pin_requires_matching_proof(tmp_path, monkeypatch):
    value = await store(tmp_path / "market.sqlite3")
    first = record()
    await value.append_many((first,))
    with pytest.raises(ValueError):
        await value.pin(first.record_id, "0" * 64)

    def broken():
        raise sqlite3.OperationalError("private-secret-path")

    monkeypatch.setattr(value, "_connect", broken)
    with pytest.raises(PersistenceUnavailable) as failure:
        await value.append_many((record(),))
    assert "private-secret" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancellation_waits_for_owned_sqlite_write_to_finish(
    tmp_path, monkeypatch, cancel_count
):
    value = await store(tmp_path / "market.sqlite3")
    entered, release = threading.Event(), threading.Event()
    original = value._connect

    def gated():
        entered.set()
        if not release.wait(2):
            raise RuntimeError("test gate timeout")
        return original()

    monkeypatch.setattr(value, "_connect", gated)
    first = record()
    pending = asyncio.create_task(value.append_many((first,)))
    assert await asyncio.to_thread(entered.wait, 2)
    try:
        for _ in range(cancel_count):
            pending.cancel()
            await asyncio.sleep(0)
        assert not pending.done(), "cancellation cannot leave owned DB thread behind"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await value.get(first.record_id) == first
