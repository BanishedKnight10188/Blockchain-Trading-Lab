"""Small durable task index; database paths are never supplied by the browser."""

import sqlite3
from contextlib import closing, contextmanager
from uuid import uuid4

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.domain.jev_tasks import JevTaskRecord
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.persistence import RequestIdentityConflict


@contextmanager
def connection(path, **options):
    with closing(sqlite3.connect(path, timeout=10, **options)) as db, db:
        yield db


class SqliteJevTaskStore:
    def __init__(self, root):
        self.root = root.resolve()
        self.path = self.root.with_suffix(".tasks.sqlite3")

    async def initialize(self):
        def work():
            with connection(self.path) as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS jev_tasks (task_id TEXT PRIMARY KEY, "
                    "request_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)"
                )

        await owned_thread(work)

    async def reserve(self, selection):
        def work():
            with connection(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT body FROM jev_tasks WHERE request_id=?", (selection.request_id,)
                ).fetchone()
                if row:
                    existing = JevTaskRecord.model_validate_json(row[0])
                    if existing.selection != selection:
                        raise RequestIdentityConflict("task creation identity conflicts")
                    return existing, False
                record = JevTaskRecord(
                    task_id=uuid4().hex,
                    request_id=selection.request_id,
                    kind=selection.kind,
                    name=selection.name.strip() or selection.analysis_target.symbol,
                    selection=selection,
                )
                db.execute(
                    "INSERT INTO jev_tasks VALUES (?,?,?)",
                    (record.task_id, record.request_id, record.model_dump_json()),
                )
                return record, True

        return await owned_thread(work)

    async def save(self, record):
        def work():
            with connection(self.path) as db:
                db.execute(
                    "UPDATE jev_tasks SET body=? WHERE task_id=?",
                    (record.model_dump_json(), record.task_id),
                )

        await owned_thread(work)

    async def all(self):
        def work():
            with connection(self.path) as db:
                return [
                    JevTaskRecord.model_validate_json(r[0])
                    for r in db.execute("SELECT body FROM jev_tasks ORDER BY rowid DESC")
                ]

        return await owned_thread(work)

    async def get(self, task_id):
        def work():
            with connection(self.path) as db:
                row = db.execute(
                    "SELECT body FROM jev_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if not row:
                    raise LookupError("task not found")
                return JevTaskRecord.model_validate_json(row[0])

        return await owned_thread(work)

    async def import_legacy(self):
        def work():
            with connection(self.root.as_uri() + "?mode=ro", uri=True) as source:
                sessions = [
                    AgentSession.model_validate_json(r[0])
                    for r in source.execute("SELECT body FROM sessions ORDER BY rowid")
                ]
            with connection(self.path) as db:
                for session in sessions:
                    if session.analysis_target.market != "usdt_perpetual":
                        continue
                    record = JevTaskRecord(
                        task_id="legacy-" + session.session_id,
                        request_id="legacy-" + session.session_id,
                        kind="jev_trader",
                        name=session.analysis_target.symbol + " · 原会话",
                        session_id=session.session_id,
                        legacy=True,
                        setup="ready",
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO jev_tasks VALUES (?,?,?)",
                        (record.task_id, record.request_id, record.model_dump_json()),
                    )

        await owned_thread(work)
