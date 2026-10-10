"""Durable typed journal; no network access and no arbitrary dictionary payloads."""

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.domain.events import AppendReceipt, EventPage, EventRecord, JournalEvent
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable

from .migrations import initialize_database


class SqliteStore:
    """EventStorePort implementation; other persistence ports are built in later steps."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._io_lock = asyncio.Lock()

    async def _io[T](self, operation: Callable[[], T]) -> T:
        async with self._io_lock:
            try:
                return await owned_thread(operation)
            except (sqlite3.Error, OSError, ValidationError):
                raise PersistenceUnavailable("local storage is unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def initialize(self) -> None:
        await self._io(lambda: initialize_database(self.path))

    @staticmethod
    def _append(connection: sqlite3.Connection, event: JournalEvent) -> AppendReceipt:
        previous = connection.execute(
            "SELECT sequence, body FROM journal_events WHERE event_id=?", (event.event_id,)
        ).fetchone()
        if previous is not None:
            if JournalEvent.model_validate_json(previous["body"]) != event:
                raise EventIdentityConflict("event identifier already refers to another fact")
            return AppendReceipt(
                event_id=event.event_id, sequence=previous["sequence"], appended=False
            )
        result = connection.execute(
            "INSERT INTO journal_events(event_id, aggregate_id, kind, occurred_at, body) "
            "VALUES(?,?,?,?,?)",
            (
                event.event_id,
                event.aggregate_id,
                event.kind.value,
                event.occurred_at.isoformat(),
                event.model_dump_json(),
            ),
        )
        return AppendReceipt(event_id=event.event_id, sequence=result.lastrowid, appended=True)

    async def append(self, event: JournalEvent) -> AppendReceipt:
        checked = JournalEvent.model_validate_json(event.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._append(connection, checked)

        return await self._io(write)

    async def scan(self, after_sequence: int, limit: int) -> EventPage:
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("journal cursor and page size must be bounded integers")

        def read():
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT sequence, body FROM journal_events WHERE sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (after_sequence, limit),
                ).fetchall()
                records = tuple(
                    EventRecord(
                        sequence=row["sequence"],
                        event=JournalEvent.model_validate_json(row["body"]),
                    )
                    for row in rows
                )
                return EventPage(
                    records=records,
                    next_sequence=records[-1].sequence if records else after_sequence,
                )

        return await self._io(read)


async def open_store(path: Path) -> SqliteStore:
    from .store import open_store as open_complete_store

    return await open_complete_store(path)
