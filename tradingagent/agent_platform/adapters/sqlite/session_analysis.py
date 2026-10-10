"""Own the initial assessment; archive public failures before bounded retries."""

import os
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.domain.session_analysis import InitialAnalysisRecord
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict


class SqliteInitialAnalysisStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._owner = None

    async def acquire_owner(self):
        """Hold an OS lock through runtime life; process exit releases it automatically."""
        if self._owner is not None:
            raise RuntimeError("initial analysis already owned")
        handle = None
        try:
            handle = self.path.with_suffix(self.path.suffix + ".initial-analysis.lock").open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if handle is not None:
                handle.close()
            raise RuntimeError("initial analysis already owned or lock unavailable") from None
        self._owner = handle

    async def release_owner(self):
        handle, self._owner = self._owner, None
        if handle is not None:
            handle.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    async def _io(self, operation):
        try:
            return await owned_thread(operation)
        except (sqlite3.Error, OSError, ValidationError):
            raise PersistenceUnavailable("initial analysis storage unavailable") from None

    async def initialize(self):
        def write():
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS session_initial_analysis ("
                    "session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), "
                    "request_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS session_initial_analysis_attempts ("
                    "session_id TEXT NOT NULL REFERENCES sessions(session_id), "
                    "request_id TEXT NOT NULL, started_at TEXT NOT NULL, status TEXT NOT NULL, "
                    "body TEXT NOT NULL, PRIMARY KEY(session_id,request_id,started_at))"
                )

        await self._io(write)

    async def get(self, session_id):
        def read():
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT body FROM session_initial_analysis WHERE session_id=?", (session_id,)
                ).fetchone()
                return InitialAnalysisRecord.model_validate_json(row[0]) if row else None

        return await self._io(read)

    async def claim(self, session, now, *, resume_prepared=False, retry_history_after=None):
        if retry_history_after is not None and (
            not isinstance(retry_history_after, timedelta)
            or retry_history_after < timedelta(seconds=60)
        ):
            raise ValueError("history retries require at least sixty seconds")
        record = InitialAnalysisRecord(
            session_id=session.session_id,
            request_id="initial:" + uuid4().hex,
            style=session.style,
            style_revision=session.style_revision,
            target=session.analysis_target,
            started_at=now,
        )

        def write():
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute(
                    "INSERT OR IGNORE INTO session_initial_analysis"
                    "(session_id,request_id,status,body) VALUES(?,?,?,?)",
                    (record.session_id, record.request_id, record.status, record.model_dump_json()),
                ).rowcount
                if changed:
                    return record
                if not resume_prepared and retry_history_after is None:
                    return None
                row = conn.execute(
                    "SELECT body FROM session_initial_analysis WHERE session_id=?",
                    (session.session_id,),
                ).fetchone()
                old = InitialAnalysisRecord.model_validate_json(row[0])
                if (
                    old.style_revision != session.style_revision
                    or old.target != session.analysis_target
                    or session.status not in ("configured", "running")
                ):
                    return None
                prepared = resume_prepared and old.status == "model_unconfigured"
                retry = (
                    retry_history_after is not None
                    and old.status == "history_unavailable"
                    and old.history is None
                    and old.result is None
                    and old.completed_at is not None
                    and now >= old.completed_at + retry_history_after
                )
                if not prepared and not retry:
                    return None
                resumed = InitialAnalysisRecord.model_validate(
                    {**record.model_dump(), "request_id": old.request_id}
                )
                conn.execute(
                    "INSERT OR IGNORE INTO session_initial_analysis_attempts "
                    "(session_id,request_id,started_at,status,body) VALUES(?,?,?,?,?)",
                    (
                        old.session_id,
                        old.request_id,
                        old.started_at.isoformat(),
                        old.status,
                        old.model_dump_json(),
                    ),
                )
                conn.execute(
                    "UPDATE session_initial_analysis SET status='pending',body=? "
                    "WHERE session_id=? AND request_id=? AND status=?",
                    (resumed.model_dump_json(), session.session_id, old.request_id, old.status),
                )
                return resumed

        return await self._io(write)

    async def finish(self, record):
        record = InitialAnalysisRecord.model_validate_json(record.model_dump_json())
        if record.status == "pending":
            raise ValueError("completion must be final")

        def write():
            with closing(self._connect()) as conn, conn:
                changed = conn.execute(
                    "UPDATE session_initial_analysis SET status=?, body=? "
                    "WHERE session_id=? AND request_id=? AND status='pending'",
                    (record.status, record.model_dump_json(), record.session_id, record.request_id),
                ).rowcount
                if changed != 1:
                    raise RevisionConflict("initial analysis attempt already finished")

        await self._io(write)

    async def recover(self, now):
        def write():
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT body FROM session_initial_analysis WHERE status='pending'"
                ).fetchall()
                for row in rows:
                    old = InitialAnalysisRecord.model_validate_json(row[0])
                    record = InitialAnalysisRecord.model_validate(
                        {
                            **old.model_dump(),
                            "status": "interrupted",
                            "completed_at": max(now, old.started_at),
                        }
                    )
                    conn.execute(
                        "UPDATE session_initial_analysis SET status=?,body=? "
                        "WHERE session_id=? AND status='pending'",
                        (record.status, record.model_dump_json(), record.session_id),
                    )

        await self._io(write)
