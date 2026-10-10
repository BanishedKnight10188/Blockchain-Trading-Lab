"""Durable command claims and monotonic backend receipts, separate from account funds."""

from contextlib import closing

from agent_platform.domain.common import required_identifier, utc_datetime
from agent_platform.domain.futures_values import FuturesQuote
from agent_platform.domain.trading_execution import (
    TERMINAL_STATUSES,
    ExecutionReceipt,
    ExecutionRecord,
    TradeCommand,
)
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import RevisionConflict
from agent_platform.ports.trading_execution import ExecutionBlocked

from .events import SqliteStore


class SqliteExecutionJournal(SqliteStore):
    async def unresolved(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                rows = db.execute(
                    "SELECT command_id FROM execution_commands WHERE account_ref=? "
                    "AND status NOT IN ('filled','rejected','canceled')",
                    (account_ref,),
                ).fetchall()
                return tuple(self._load(db, row[0]) for row in rows)

        return await self._io(read)

    async def initialize(self):
        await super().initialize()

        def create():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS execution_commands ("
                    "command_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, "
                    "revision INTEGER NOT NULL CHECK(revision>0), status TEXT NOT NULL, "
                    "body TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS execution_commands_account "
                    "ON execution_commands(account_ref,status)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS execution_updates ("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL "
                    "REFERENCES execution_commands(command_id), body TEXT NOT NULL)"
                )

        await self._io(create)

    @staticmethod
    def _load(db, command_id):
        row = db.execute(
            "SELECT * FROM execution_commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            return None
        record = ExecutionRecord.model_validate_json(row["body"])
        if (
            record.command.command_id != row["command_id"]
            or record.command.scope.account_ref != row["account_ref"]
            or record.revision != row["revision"]
            or record.receipt.status != row["status"]
        ):
            raise ValueError("execution projection disagrees with its stored facts")
        return record

    @staticmethod
    def _write(db, record):
        db.execute(
            "INSERT INTO execution_commands(command_id,account_ref,revision,status,body) "
            "VALUES(?,?,?,?,?) ON CONFLICT(command_id) DO UPDATE SET "
            "revision=excluded.revision,status=excluded.status,body=excluded.body",
            (
                record.command.command_id,
                record.command.scope.account_ref,
                record.revision,
                record.receipt.status,
                record.model_dump_json(),
            ),
        )
        db.execute(
            "INSERT INTO execution_updates(command_id,body) VALUES(?,?)",
            (record.command.command_id, record.model_dump_json()),
        )

    async def reserve(self, command, at):
        command = TradeCommand.model_validate(command)
        at = utc_datetime(at)

        def claim():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                prior = self._load(db, command.command_id)
                if prior is not None:
                    if prior.command != command:
                        raise EventIdentityConflict("command ID already names different inputs")
                    return prior, False
                if not command.created_at <= at <= command.expires_at:
                    raise ValueError("new execution command is outside its lifetime")
                blocked = db.execute(
                    "SELECT command_id FROM execution_commands WHERE account_ref=? "
                    "AND status NOT IN ('filled','rejected','canceled')",
                    (command.scope.account_ref,),
                ).fetchall()
                e = command.decision_evidence
                protective = (
                    getattr(e, "kind", None) == "guardian"
                    and command.action == "reduce"
                    and all(row[0] == "agent:" + e.protection_id for row in blocked)
                )
                if blocked and not protective:
                    raise ExecutionBlocked("resolve the current account command first")
                record = ExecutionRecord(
                    command=command,
                    receipt=ExecutionReceipt(command=command, status="pending", observed_at=at),
                )
                self._write(db, record)
                return record, True

        return await self._io(claim)

    async def get(self, command_id):
        command_id = required_identifier(command_id)

        def read():
            with closing(self._connect()) as db:
                result = self._load(db, command_id)
                if result is None:
                    raise LookupError("execution command is unavailable")
                return result

        return await self._io(read)

    @staticmethod
    def _progress(before, after):
        if before.status in TERMINAL_STATUSES:
            raise EventIdentityConflict("terminal execution facts cannot be replaced")
        if (
            after.observed_at < before.observed_at
            or after.filled_quantity < before.filled_quantity
            or after.fee_usdt < before.fee_usdt
            or (
                before.backend_at is not None
                and (after.backend_at is None or after.backend_at < before.backend_at)
            )
            or (
                before.backend_order_id is not None
                and after.backend_order_id != before.backend_order_id
            )
            or (
                before.filled_quantity > 0
                and before.filled_quantity == after.filled_quantity
                and before.average_price != after.average_price
            )
            or (before.status != "pending" and after.status == "pending")
        ):
            raise EventIdentityConflict("execution observations cannot regress or change identity")

    async def save(self, receipt, expected_revision, *, quote=None):
        receipt = ExecutionReceipt.model_validate(receipt)
        quote = FuturesQuote.model_validate(quote) if quote is not None else None
        if type(expected_revision) is not int or expected_revision < 1:
            raise RevisionConflict("execution revision must be a positive integer")

        def update():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                prior = self._load(db, receipt.command.command_id)
                if prior is None:
                    raise LookupError("persist command before observing execution")
                if prior.command != receipt.command:
                    raise EventIdentityConflict("receipt refers to different command inputs")
                if prior.receipt == receipt and (quote is None or quote == prior.quote):
                    return prior
                if prior.revision != expected_revision:
                    raise RevisionConflict("execution command changed; reload before observing")
                self._progress(prior.receipt, receipt)
                if quote is not None and prior.quote is not None and quote != prior.quote:
                    raise EventIdentityConflict("submitted execution evidence is immutable")
                record = ExecutionRecord(
                    command=prior.command,
                    receipt=receipt,
                    revision=prior.revision + 1,
                    quote=quote if quote is not None else prior.quote,
                )
                self._write(db, record)
                return record

        return await self._io(update)
