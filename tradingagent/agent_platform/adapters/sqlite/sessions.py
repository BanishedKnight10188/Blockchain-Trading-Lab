"""SQLite WAL session state and audit written in one transaction."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.domain.events import SessionJournalEvent
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.sessions import (
    ActiveSessionExists,
    PersistenceUnavailable,
    RevisionConflict,
    SessionNotFound,
)

from .migrations import initialize_database


class SqliteSessionStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    async def _io[T](self, operation: Callable[[], T]) -> T:
        try:
            return await owned_thread(operation)
        except (sqlite3.Error, OSError, ValidationError):
            raise PersistenceUnavailable("session storage is unavailable") from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def initialize(self) -> None:
        await self._io(lambda: initialize_database(self.path))

    async def active(self) -> AgentSession | None:
        def read():
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT body FROM sessions WHERE status <> 'closed'",
                ).fetchone()
                return AgentSession.model_validate_json(row[0]) if row else None

        return await self._io(read)

    async def get(self, session_id: str) -> AgentSession:
        def read():
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT body FROM sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise SessionNotFound("session not found")
                return AgentSession.model_validate_json(row[0])

        return await self._io(read)

    @staticmethod
    def _append(connection: sqlite3.Connection, event: SessionJournalEvent) -> None:
        connection.execute(
            "INSERT INTO session_events(event_id, session_id, revision, body) VALUES(?,?,?,?)",
            (
                event.event_id,
                event.session.session_id,
                event.session.revision,
                event.model_dump_json(),
            ),
        )

    @staticmethod
    def _validate_session_change(original: AgentSession, session: AgentSession, kind: str) -> None:
        if kind == "style_changed":
            expected = original.change_style(session.style, session.updated_at)
        elif kind == "state_changed":
            expected = original.transition(session.status, session.updated_at)
        else:
            raise ValueError("session update requires a change event")
        if session != expected:
            raise ValueError("session update must be a single valid domain transition")

    async def create(self, session: AgentSession, event: SessionJournalEvent) -> AgentSession:
        session = AgentSession.model_validate_json(session.model_dump_json())
        event = SessionJournalEvent.model_validate_json(event.model_dump_json())
        if session != event.session or event.kind != "created" or session.revision != 1:
            raise ValueError("creation event must match initial session")
        if session.status != "configured":
            raise ValueError("initial session must be configured")

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                occupied = connection.execute(
                    "SELECT 1 FROM sessions WHERE status <> 'closed'",
                ).fetchone()
                if occupied:
                    raise ActiveSessionExists("an open session already exists")
                connection.execute(
                    "INSERT INTO sessions(session_id, revision, status, body) VALUES(?,?,?,?)",
                    (
                        session.session_id,
                        session.revision,
                        session.status.value,
                        session.model_dump_json(),
                    ),
                )
                self._append(connection, event)
            return session

        return await self._io(write)

    async def save(
        self,
        session: AgentSession,
        expected_revision: int,
        event: SessionJournalEvent,
    ) -> AgentSession:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected session revision must be a positive integer")
        session = AgentSession.model_validate_json(session.model_dump_json())
        event = SessionJournalEvent.model_validate_json(event.model_dump_json())
        if (
            session != event.session
            or event.kind not in ("style_changed", "state_changed")
            or session.revision != expected_revision + 1
        ):
            raise ValueError("change event must match next session revision")

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT body FROM sessions WHERE session_id=? AND revision=?",
                    (session.session_id, expected_revision),
                ).fetchone()
                if previous is None:
                    raise RevisionConflict("session changed; reload before saving")
                self._validate_session_change(
                    AgentSession.model_validate_json(previous["body"]), session, event.kind
                )
                result = connection.execute(
                    "UPDATE sessions SET revision=?, status=?, body=? "
                    "WHERE session_id=? AND revision=?",
                    (
                        session.revision,
                        session.status.value,
                        session.model_dump_json(),
                        session.session_id,
                        expected_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise RevisionConflict("session changed; reload before saving")
                self._append(connection, event)
            return session

        return await self._io(write)

    async def history(self, session_id: str) -> tuple[SessionJournalEvent, ...]:
        def read():
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT body FROM session_events WHERE session_id=? ORDER BY sequence",
                    (session_id,),
                ).fetchall()
                return tuple(SessionJournalEvent.model_validate_json(row[0]) for row in rows)

        return await self._io(read)
