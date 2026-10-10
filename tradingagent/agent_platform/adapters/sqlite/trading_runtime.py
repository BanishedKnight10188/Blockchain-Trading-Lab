"""Durable run configuration, funding schedule and decision history; one OS owner."""

import os
from contextlib import closing

from agent_platform.domain.trading_runtime import FundingCheckpoint, TradingCycle, TradingRun
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .events import SqliteStore


class SqliteTradingRuntimeStore(SqliteStore):
    def __init__(self, path):
        super().__init__(path)
        self._owner = None

    async def acquire_owner(self):
        if self._owner is not None:
            raise RuntimeError("trading runtime already owned")
        handle = None
        try:
            handle = self.path.with_suffix(self.path.suffix + ".futures-runtime.lock").open("a+b")
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
            raise RuntimeError("another process owns the futures runtime") from None
        self._owner = handle

    async def release_owner(self):
        handle, self._owner = self._owner, None
        if handle is not None:
            handle.close()

    async def initialize(self):
        await super().initialize()

        def write():
            with closing(self._connect()) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_trading_runs ("
                    "account_ref TEXT PRIMARY KEY, body TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_funding_checkpoints ("
                    "account_ref TEXT PRIMARY KEY, body TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_trading_cycles ("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE "
                    "NOT NULL, account_ref TEXT NOT NULL, status TEXT NOT NULL, body TEXT "
                    "NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_prediction_heads ("
                    "account_ref TEXT PRIMARY KEY, sequence INTEGER NOT NULL)"
                )

        await self._io(write)

    async def configure(self, run):
        run = TradingRun.model_validate_json(run.model_dump_json())

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT body FROM futures_trading_runs WHERE account_ref=?",
                    (run.scope.account_ref,),
                ).fetchone()
                if old and TradingRun.model_validate_json(old[0]) != run:
                    raise EventIdentityConflict("run policy is fixed for this virtual wallet")
                db.execute(
                    "INSERT OR IGNORE INTO futures_trading_runs VALUES(?,?)",
                    (run.scope.account_ref, run.model_dump_json()),
                )

        await self._io(write)

    async def run(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT body FROM futures_trading_runs WHERE account_ref=?", (account_ref,)
                ).fetchone()
                return TradingRun.model_validate_json(row[0]) if row else None

        return await self._io(read)

    async def checkpoint(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT body FROM futures_funding_checkpoints WHERE account_ref=?",
                    (account_ref,),
                ).fetchone()
                return FundingCheckpoint.model_validate_json(row[0]) if row else None

        return await self._io(read)

    async def save_checkpoint(self, checkpoint):
        checkpoint = FundingCheckpoint.model_validate_json(checkpoint.model_dump_json())

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT body FROM futures_funding_checkpoints WHERE account_ref=?",
                    (checkpoint.account_ref,),
                ).fetchone()
                if row and checkpoint.cursor < FundingCheckpoint.model_validate_json(row[0]).cursor:
                    raise RevisionConflict("funding cursor cannot move backwards")
                db.execute(
                    "INSERT INTO futures_funding_checkpoints VALUES(?,?) ON CONFLICT "
                    "(account_ref) DO UPDATE SET body=excluded.body",
                    (checkpoint.account_ref, checkpoint.model_dump_json()),
                )

        await self._io(write)

    async def claim(self, cycle, *, max_pending=1):
        if type(max_pending) is not int or not 1 <= max_pending <= 3:
            raise ValueError("prediction capacity must be 1..3")
        cycle = TradingCycle.model_validate_json(cycle.model_dump_json())
        if cycle.status != "pending":
            raise ValueError("a new decision must be pending")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM futures_trading_cycles "
                        "WHERE account_ref=? AND status='pending'",
                        (cycle.scope.account_ref,),
                    ).fetchone()[0]
                    >= max_pending
                ):
                    raise RevisionConflict("prediction capacity reached")
                db.execute(
                    "INSERT INTO futures_trading_cycles(request_id,account_ref,status,body) "
                    "VALUES(?,?,?,?)",
                    (
                        cycle.request_id,
                        cycle.scope.account_ref,
                        cycle.status,
                        cycle.model_dump_json(),
                    ),
                )

        await self._io(write)

    async def accept_result(self, request_id):
        """Advance a durable result barrier before releasing the funds gate.

        Pending newer requests do not invalidate usable older responses. A newer
        accepted verdict does, including WAIT, even before its cycle is finished.
        """

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT account_ref,sequence,status FROM futures_trading_cycles "
                    "WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if row is None or row[2] != "pending":
                    raise RevisionConflict("prediction unavailable")
                head = db.execute(
                    "SELECT sequence FROM futures_prediction_heads WHERE account_ref=?", (row[0],)
                ).fetchone()
                if head is not None and head[0] >= row[1]:
                    return False
                db.execute(
                    "INSERT INTO futures_prediction_heads VALUES(?,?) "
                    "ON CONFLICT(account_ref) DO UPDATE SET sequence=excluded.sequence",
                    (row[0], row[1]),
                )
                return True

        return await self._io(write)

    async def prepare(self, cycle):
        if cycle.status != "pending":
            raise ValueError("prepared decision must still be pending")
        await self._update(cycle)

    async def finish(self, cycle):
        cycle = TradingCycle.model_validate_json(cycle.model_dump_json())
        if cycle.status == "pending" or cycle.completed_at is None:
            raise ValueError("decision completion must be final")
        await self._update(cycle)

    async def _update(self, cycle):
        cycle = TradingCycle.model_validate_json(cycle.model_dump_json())

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT body FROM futures_trading_cycles WHERE request_id=?",
                    (cycle.request_id,),
                ).fetchone()
                if row is None:
                    raise RevisionConflict("decision is unavailable")
                old = TradingCycle.model_validate_json(row[0])
                if old.model_request is not None and old.model_request != cycle.model_request:
                    raise EventIdentityConflict("original model question cannot change")
                if old.model_response is not None and old.model_response != cycle.model_response:
                    raise EventIdentityConflict("original model answer cannot change")
                for key in (
                    "request_id",
                    "scope",
                    "style_revision",
                    "trader_revision",
                    "account_revision",
                    "created_at",
                    "quote",
                ):
                    if getattr(old, key) != getattr(cycle, key):
                        raise EventIdentityConflict("decision identity cannot change")
                count = db.execute(
                    "UPDATE futures_trading_cycles SET status=?,body=? "
                    "WHERE request_id=? AND status='pending'",
                    (cycle.status, cycle.model_dump_json(), cycle.request_id),
                ).rowcount
                if count != 1:
                    raise RevisionConflict("decision already completed")

        await self._io(write)

    async def recent(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                rows = db.execute(
                    "SELECT body FROM futures_trading_cycles WHERE account_ref=? "
                    "ORDER BY sequence DESC LIMIT 50",
                    (account_ref,),
                ).fetchall()
                return tuple(TradingCycle.model_validate_json(r[0]) for r in rows)

        return await self._io(read)

    async def recover(self, at):
        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute(
                    "SELECT body FROM futures_trading_cycles WHERE status='pending'"
                ).fetchall()
                for row in rows:
                    c = TradingCycle.model_validate_json(row[0])
                    c = TradingCycle.model_validate(
                        c.model_dump()
                        | {
                            "status": "interrupted",
                            "reason": "process_restarted",
                            "completed_at": max(at, c.created_at),
                        }
                    )
                    db.execute(
                        "UPDATE futures_trading_cycles SET status=?,body=? WHERE request_id=?",
                        (c.status, c.model_dump_json(), c.request_id),
                    )

        await self._io(write)
