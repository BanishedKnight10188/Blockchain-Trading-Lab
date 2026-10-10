"""Operational artifacts are bounded, safe, and never overwrite user files."""

import asyncio
import importlib
import json
import sqlite3
import subprocess
import sys

import pytest

from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.schema import SCHEMA_VERSION
from agent_platform.domain.health import RuntimeHealth, WorkerHealth
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.adapters.test_sqlite_events import event
from tests.domain.test_decisions import NOW


def log_event():
    cls = importlib.import_module("agent_platform.domain.operations").OperationalEvent
    return cls(
        event="worker_health",
        occurred_at=NOW,
        health=RuntimeHealth(
            status="degraded",
            worker_count=1,
            workers=(WorkerHealth(name="archive", running=False, failure="persistence"),),
        ),
    )


@pytest.mark.asyncio
async def test_safe_log_rejects_external_text_and_rotates_to_bounded_files(tmp_path):
    cls = importlib.import_module("agent_platform.adapters.operational_log").SafeOperationalLog
    value = log_event()
    with pytest.raises(ValueError):
        type(value)(event="worker_health", occurred_at=NOW, health=value.health, secret="private")
    with pytest.raises(ValueError):
        type(value)(event="private-error-secret", occurred_at=NOW, health=value.health)
    path = tmp_path / "operations.jsonl"
    logger = cls(path, max_bytes=1024, backup_count=3)
    for _ in range(40):
        await logger.write(value)
    files = tuple(tmp_path.iterdir())
    assert len(files) == 4 and all(item.stat().st_size <= 1024 for item in files)
    for item in files:
        for line in item.read_text(encoding="utf-8").splitlines():
            data = json.loads(line)
            assert set(data) == {"schema_version", "event", "occurred_at", "health"}
            assert data["health"]["workers"][0]["failure"] == "persistence"


@pytest.mark.asyncio
async def test_log_disk_error_is_fixed_and_not_silently_reported_as_success(tmp_path):
    cls = importlib.import_module("agent_platform.adapters.operational_log").SafeOperationalLog
    path = tmp_path / "directory"
    path.mkdir()
    logger = cls(path)
    with pytest.raises(PersistenceUnavailable) as failure:
        await logger.write(log_event())
    assert "directory" not in str(failure.value)


@pytest.mark.asyncio
async def test_online_core_backup_preserves_live_wal_and_refuses_overwrite(tmp_path):
    copy = importlib.import_module("agent_platform.adapters.sqlite.backup").backup_database
    path, target = tmp_path / "core.sqlite3", tmp_path / "backup.sqlite3"
    store = await open_store(path)
    with sqlite3.connect(path) as held:
        held.execute("PRAGMA wal_autocheckpoint=0")
        await store.append(event())
        result = await copy(path, target, kind="core")
        assert result["schema_version"] == 1 and result["source_schema_version"] == SCHEMA_VERSION
        assert result["journal_events"] == 1 and result["integrity"] == "ok"
        assert len(result["sha256"]) == 64 and result["bytes"] > 0
    reopened = await open_store(target)
    assert (await reopened.scan(0, 10)).records[0].event == event()
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        await copy(path, target, kind="core")
    assert target.read_bytes() == before
    with pytest.raises(ValueError):
        await copy(path, path, kind="core")


@pytest.mark.asyncio
async def test_backup_rejects_missing_source_or_wrong_database_kind_before_target_creation(
    tmp_path,
):
    copy = importlib.import_module("agent_platform.adapters.sqlite.backup").backup_database
    target = tmp_path / "unused.sqlite3"
    with pytest.raises(PersistenceUnavailable):
        await copy(tmp_path / "missing.sqlite3", target, kind="core")
    assert not target.exists() and not (tmp_path / "missing.sqlite3").exists()
    path = tmp_path / "core.sqlite3"
    await open_store(path)
    with pytest.raises(ValueError):
        await copy(path, target, kind="market")
    assert not target.exists()


@pytest.mark.asyncio
async def test_backup_cli_runs_without_account_config_and_prints_verified_report(tmp_path):
    path, target = tmp_path / "cli.sqlite3", tmp_path / "copy.sqlite3"
    await open_store(path)
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "agent_platform.cli",
            "backup",
            "--database",
            str(path),
            "--output",
            str(target),
            "--kind",
            "core",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["integrity"] == "ok"


@pytest.mark.asyncio
async def test_market_backup_refuses_mixed_core_tables_before_reserving_output(tmp_path):
    from agent_platform.adapters.sqlite.retention import SqliteMarketArchive

    copy = importlib.import_module("agent_platform.adapters.sqlite.backup").backup_database
    path, target = tmp_path / "mixed.sqlite3", tmp_path / "unused.sqlite3"
    await SqliteMarketArchive(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE session_events(account_ref TEXT)")
        connection.execute("INSERT INTO session_events VALUES('private-fixture')")
    with pytest.raises(ValueError, match="schema"):
        await copy(path, target, kind="market")
    assert not target.exists()
