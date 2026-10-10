"""Cancellation waits for actual local IO completion, including repeated cancellation."""

import asyncio
import sqlite3
import threading

import pytest

from agent_platform.adapters.sqlite.events import SqliteStore
from agent_platform.adapters.sqlite.retention import SqliteMarketArchive
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", [SqliteStore, SqliteSessionStore, SqliteMarketArchive])
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_io_retains_thread_ownership_until_commit(tmp_path, adapter, cancel_count):
    path = tmp_path / "owned.sqlite3"
    store = adapter(path)
    await store.initialize()
    started, release = threading.Event(), threading.Event()

    def write():
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE ownership_probe (value INTEGER)")
            started.set()
            if not release.wait(2):
                raise RuntimeError("test gate expired")
            connection.execute("INSERT INTO ownership_probe VALUES(1)")

    task = asyncio.create_task(store._io(write))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), "the actual SQLite transaction is still owned"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM ownership_probe").fetchone()[0] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
