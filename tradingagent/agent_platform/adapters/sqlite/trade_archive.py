"""Atomic per-operation archive and verified, account-scoped export snapshots."""

import csv
import io
import json
from contextlib import closing
from hashlib import sha256
from tempfile import SpooledTemporaryFile
from typing import Literal

from pydantic import Field

from agent_platform.domain.futures_paper import FuturesPaperRecord
from agent_platform.domain.models import DomainModel

from .events import SqliteStore

GENESIS = "0" * 64
ARCHIVED_KINDS = ("trade", "funding", "liquidation")


class TradeArchiveEntry(DomainModel):
    sequence: int = Field(strict=True, ge=1)
    origin: Literal["recorded", "legacy_import"]
    record: FuturesPaperRecord
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class TradeArchivePage(DomainModel):
    entries: tuple[TradeArchiveEntry, ...]
    total: int = Field(strict=True, ge=0)
    through: int = Field(strict=True, ge=0)
    next_after: int | None = Field(default=None, strict=True, ge=1)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(payload):
    return sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _checked(row, previous_hash):
    try:
        raw = json.loads(row["body"])
        digest = raw.pop("content_hash")
        if (
            raw["previous_hash"] != previous_hash
            or digest != _hash(raw)
            or digest != row["content_hash"]
            or raw["previous_hash"] != row["previous_hash"]
            or raw["sequence"] != row["sequence"]
            or raw["record"]["command_id"] != row["command_id"]
            or raw["record"]["account_ref"] != row["account_ref"]
        ):
            raise ValueError("trade_archive_integrity")
        return TradeArchiveEntry.model_validate_json(_canonical(raw | {"content_hash": digest}))
    except (ValueError, KeyError, TypeError):
        raise ValueError("trade_archive_integrity") from None


def append_archive(db, record, sequence, *, origin="recorded"):
    """Use the wallet's existing write transaction; failure rolls back the fill."""
    if record.kind not in ARCHIVED_KINDS:
        return
    if origin == "recorded" and record.before_state is None:
        raise ValueError("trade_archive_before_state_missing")
    previous = db.execute(
        "SELECT * FROM futures_trade_archive WHERE account_ref=? ORDER BY sequence DESC LIMIT 1",
        (record.account_ref,),
    ).fetchone()
    previous_hash = GENESIS
    if previous is not None:
        _checked(previous, previous["previous_hash"])
        if sequence <= previous["sequence"]:
            raise ValueError("trade_archive_sequence")
        previous_hash = previous["content_hash"]
    payload = {
        "schema_version": 1,
        "sequence": sequence,
        "origin": origin,
        "record": record.model_dump(mode="json"),
        "previous_hash": previous_hash,
    }
    digest = _hash(payload)
    entry = TradeArchiveEntry.model_validate_json(_canonical(payload | {"content_hash": digest}))
    db.execute(
        "INSERT INTO futures_trade_archive(sequence,command_id,account_ref,previous_hash,"
        "content_hash,body) VALUES(?,?,?,?,?,?)",
        (
            sequence,
            record.command_id,
            record.account_ref,
            previous_hash,
            digest,
            entry.model_dump_json(),
        ),
    )


def initialize_archive(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS futures_trade_archive (sequence INTEGER PRIMARY KEY "
        "REFERENCES futures_paper_operations(sequence), command_id TEXT UNIQUE NOT NULL "
        "REFERENCES futures_paper_operations(command_id), account_ref TEXT NOT NULL "
        "REFERENCES futures_paper_wallets(account_ref), previous_hash TEXT NOT NULL, "
        "content_hash TEXT NOT NULL, body TEXT NOT NULL)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS futures_trade_archive_account "
        "ON futures_trade_archive(account_ref,sequence)"
    )
    # New archives and their original operation facts are append-only.
    for table in ("futures_trade_archive", "futures_paper_operations"):
        for action in ("update", "delete"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action} "
                f"BEFORE {action.upper()} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'trade archive is immutable'); END"
            )
    rows = db.execute(
        "SELECT o.sequence,o.body FROM futures_paper_operations o "
        "LEFT JOIN futures_trade_archive a ON a.command_id=o.command_id "
        "WHERE a.command_id IS NULL AND json_extract(o.body,'$.kind') "
        "IN ('trade','funding','liquidation') ORDER BY o.sequence"
    ).fetchall()
    for row in rows:
        append_archive(
            db,
            FuturesPaperRecord.model_validate_json(row["body"]),
            row["sequence"],
            origin="legacy_import",
        )


class SqliteTradeArchiveStore(SqliteStore):
    @staticmethod
    def _bounds(db, account_ref, through=None):
        if not db.execute(
            "SELECT 1 FROM futures_paper_wallets WHERE account_ref=?", (account_ref,)
        ).fetchone():
            raise LookupError("trade_archive_account_unavailable")
        current = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM futures_paper_operations WHERE account_ref=?",
            (account_ref,),
        ).fetchone()[0]
        if through is not None and (type(through) is not int or not 0 <= through <= current):
            raise ValueError("trade_archive_cursor")
        return current if through is None else through

    @staticmethod
    def _verified(db, account_ref, through):
        """Walk original operations too: missing archive rows cannot silently disappear."""
        previous_hash = GENESIS
        rows = db.execute(
            "SELECT a.*,o.body AS original_body,o.command_id AS original_command "
            "FROM futures_paper_operations o LEFT JOIN futures_trade_archive a "
            "ON a.sequence=o.sequence WHERE o.account_ref=? AND o.sequence<=? "
            "AND json_extract(o.body,'$.kind') IN ('trade','funding','liquidation') "
            "ORDER BY o.sequence",
            (account_ref, through),
        )
        for row in rows:
            if row["body"] is None:
                raise ValueError("trade_archive_missing")
            entry = _checked(row, previous_hash)
            original = FuturesPaperRecord.model_validate_json(row["original_body"])
            if entry.record != original or row["command_id"] != row["original_command"]:
                raise ValueError("trade_archive_operation_mismatch")
            previous_hash = entry.content_hash
            yield entry

    async def summary(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                db.execute("BEGIN")
                through = self._bounds(db, account_ref)
                row = db.execute(
                    "SELECT COUNT(*),COALESCE(MAX(sequence),0), "
                    "COALESCE(SUM(json_extract(body,'$.origin')='legacy_import'),0), "
                    "COALESCE(SUM(json_extract(body,'$.record.kind')='trade'),0), "
                    "COALESCE(SUM(json_extract(body,'$.record.kind')='funding'),0), "
                    "COALESCE(SUM(json_extract(body,'$.record.kind')='liquidation'),0) "
                    "FROM futures_trade_archive WHERE account_ref=? AND sequence<=?",
                    (account_ref, through),
                ).fetchone()
                return {
                    "total": row[0],
                    "last_sequence": row[1],
                    "legacy_count": row[2],
                    "counts": {"trade": row[3], "funding": row[4], "liquidation": row[5]},
                    "atomic": True,
                    "hash_algorithm": "sha256",
                    "currency": "USDT",
                }

        return await self._io(read)

    async def page(self, account_ref, *, after=0, limit=50, through=None):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("trade_archive_cursor")

        def read():
            with closing(self._connect()) as db:
                db.execute("BEGIN")
                high = self._bounds(db, account_ref, through)
                if after > high:
                    raise ValueError("trade_archive_cursor")
                entries, total, more = [], 0, False
                for entry in self._verified(db, account_ref, high):
                    total += 1
                    if entry.sequence > after:
                        if len(entries) < limit:
                            entries.append(entry)
                        else:
                            more = True
                return TradeArchivePage(
                    entries=tuple(entries),
                    total=total,
                    through=high,
                    next_after=entries[-1].sequence if more else None,
                )

        return await self._io(read)

    async def export(self, account_ref, *, format="jsonl"):
        if format not in ("jsonl", "csv"):
            raise ValueError("trade_archive_format")

        def read():
            output = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
            try:
                with closing(self._connect()) as db:
                    db.execute("BEGIN")
                    high = self._bounds(db, account_ref)
                    if format == "jsonl":
                        output.write(
                            (
                                _canonical(
                                    {
                                        "type": "manifest",
                                        "schema_version": 1,
                                        "account_ref": account_ref,
                                        "through": high,
                                        "hash_algorithm": "sha256",
                                    }
                                )
                                + "\n"
                            ).encode()
                        )
                    else:
                        output.write(b"\xef\xbb\xbf")
                        output.write(_csv_line(CSV_COLUMNS))
                    count, last_hash = 0, GENESIS
                    for entry in self._verified(db, account_ref, high):
                        if format == "jsonl":
                            output.write(
                                (
                                    _canonical(
                                        {
                                            "type": "operation",
                                            "entry": entry.model_dump(mode="json"),
                                        }
                                    )
                                    + "\n"
                                ).encode()
                            )
                        else:
                            output.write(_csv_line(_csv_values(entry)))
                        count += 1
                        last_hash = entry.content_hash
                    if format == "jsonl":
                        output.write(
                            (
                                _canonical(
                                    {
                                        "type": "complete",
                                        "count": count,
                                        "last_hash": last_hash,
                                        "through": high,
                                    }
                                )
                                + "\n"
                            ).encode()
                        )
                output.seek(0)
                return output
            except BaseException:
                output.close()
                raise

        return await self._io(read)


CSV_COLUMNS = (
    "sequence",
    "time_utc",
    "symbol",
    "kind",
    "origin",
    "decision_source",
    "intent",
    "sizing_basis",
    "percent",
    "quantity",
    "price",
    "side",
    "leverage_before",
    "leverage_after",
    "fee_usdt",
    "realized_pnl_usdt",
    "funding_usdt",
    "free_before",
    "free_after",
    "margin_before",
    "margin_after",
    "position_before",
    "position_after",
    "request_id",
    "model",
    "confidence",
    "model_latency_ms",
    "model_cost_usd",
    "billing_status",
    "command_id",
    "previous_hash",
    "content_hash",
)


def _csv_line(values):
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(values)
    return buffer.getvalue().encode("utf-8")


def _csv_values(entry):
    r, op, before = entry.record, entry.record.operation, entry.record.before_state
    evidence = r.execution_command.decision_evidence if r.execution_command else None
    plan = evidence.plan if evidence else None
    answer = evidence.response.answers[0] if evidence else None
    usage = evidence.response.usage if evidence else None
    return (
        entry.sequence,
        op.occurred_at.isoformat(),
        r.state.symbol,
        r.kind,
        entry.origin,
        evidence.decision_source if evidence else "unavailable",
        plan.intent if plan else "",
        plan.sizing_basis if plan else "",
        plan.percent if plan else "",
        str(op.quantity),
        str(op.price) if op.price else "",
        op.side or "",
        before.settings.leverage if before else "",
        r.state.settings.leverage,
        str(op.fee_usdt),
        str(op.realized_pnl_usdt),
        str(op.funding_usdt),
        str(before.free_usdt) if before else "",
        str(r.state.free_usdt),
        str(before.margin_usdt) if before else "",
        str(r.state.margin_usdt),
        str(before.quantity) if before else "",
        str(r.state.quantity),
        evidence.request.request_id if evidence else "",
        evidence.response.provider_metadata.model_id if evidence else "",
        str(answer.confidence) if answer else "",
        evidence.elapsed_ms if evidence else "",
        str(usage.actual_cost_usd) if usage and usage.actual_cost_usd is not None else "",
        usage.billing_status if usage else "",
        r.command_id,
        entry.previous_hash,
        entry.content_hash,
    )
