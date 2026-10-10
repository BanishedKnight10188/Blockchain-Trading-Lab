"""Separate SQLite sidecar: bounded configured retention, explicit permanent pins."""

import asyncio
import calendar
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.domain.common import required_identifier, utc_datetime
from agent_platform.domain.market_archive import (
    MarketArchiveRecord,
    MarketRetentionPolicy,
    RetentionReport,
)
from agent_platform.ports.persistence import ObservationConflict
from agent_platform.ports.sessions import PersistenceUnavailable

_TABLE = (
    "CREATE TABLE market_archive ("
    "record_id TEXT PRIMARY KEY NOT NULL, "
    "kind TEXT NOT NULL CHECK(kind IN ('raw','minute')), "
    "event_seconds INTEGER NOT NULL, event_microseconds INTEGER NOT NULL "
    "CHECK(event_microseconds BETWEEN 0 AND 999999), "
    "body TEXT NOT NULL, source_hash TEXT NOT NULL, "
    "pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)))"
)


def _time(at):
    return calendar.timegm(at.utctimetuple()), at.microsecond


class SqliteMarketArchive:
    def __init__(self, path: Path, *, retention: MarketRetentionPolicy | None = None):
        if retention is not None and not isinstance(retention, MarketRetentionPolicy):
            raise ValueError("archive retention requires an owned policy")
        self.retention = MarketRetentionPolicy.model_validate(
            retention.model_dump() if retention is not None else {}
        )
        self.path = Path(path).resolve()
        self._lock = asyncio.Lock()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    async def _io(self, operation):
        async with self._lock:
            try:
                return await owned_thread(operation)
            except FileExistsError:
                raise FileExistsError("backup target already exists") from None
            except (sqlite3.Error, OSError, ValidationError):
                raise PersistenceUnavailable("auxiliary market storage unavailable") from None

    async def initialize(self):
        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if version not in (0, 1) or tables not in (set(), {"market_archive"}):
                    raise ValueError("archive requires a separate supported database")
                if not tables:
                    connection.execute(_TABLE)
                stored_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='market_archive'"
                ).fetchone()[0]
                if stored_sql != _TABLE:
                    raise ValueError("archive schema is unsupported")
                columns = connection.execute("PRAGMA table_info(market_archive)").fetchall()
                if (
                    tuple(row["name"] for row in columns)
                    != (
                        "record_id",
                        "kind",
                        "event_seconds",
                        "event_microseconds",
                        "body",
                        "source_hash",
                        "pinned",
                    )
                    or columns[0]["pk"] != 1
                ):
                    raise ValueError("archive schema is unsupported")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS archive_expiry "
                    "ON market_archive(pinned,kind,event_seconds,event_microseconds)"
                )
                connection.execute("PRAGMA user_version=1")

        await self._io(write)

    @staticmethod
    def _decode(row):
        value = MarketArchiveRecord.model_validate_json(row["body"])
        if (
            value.record_id != row["record_id"]
            or value.source_hash != row["source_hash"]
            or value.kind != row["kind"]
            or _time(value.event_at) != (row["event_seconds"], row["event_microseconds"])
        ):
            raise PersistenceUnavailable("auxiliary market proof is invalid")
        return value

    async def append_many(self, records):
        if not isinstance(records, tuple) or len(records) > 121:
            raise ValueError("archive batch supports at most 121 observations")
        checked = tuple(
            MarketArchiveRecord.model_validate_json(value.model_dump_json()) for value in records
        )

        def write():
            results = []
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                for value in checked:
                    prior = connection.execute(
                        "SELECT * FROM market_archive WHERE record_id=?", (value.record_id,)
                    ).fetchone()
                    if prior is not None:
                        original = self._decode(prior)
                        if original.source_hash != value.source_hash:
                            raise ObservationConflict(
                                "archive identity refers to another observation"
                            )
                        results.append(False)
                    else:
                        connection.execute(
                            "INSERT INTO market_archive(record_id,kind,event_seconds,"
                            "event_microseconds,body,source_hash) VALUES(?,?,?,?,?,?)",
                            (
                                value.record_id,
                                value.kind,
                                *_time(value.event_at),
                                value.model_dump_json(),
                                value.source_hash,
                            ),
                        )
                        results.append(True)
            return tuple(results)

        return await self._io(write)

    async def get(self, record_id):
        identity = required_identifier(record_id)

        def read():
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM market_archive WHERE record_id=?", (identity,)
                ).fetchone()
                return self._decode(row) if row else None

        return await self._io(read)

    async def pin(self, record_id, source_hash):
        identity = required_identifier(record_id)

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM market_archive WHERE record_id=?", (identity,)
                ).fetchone()
                if row is None or self._decode(row).source_hash != source_hash:
                    raise ValueError("pin requires an existing matching observation")
                connection.execute(
                    "UPDATE market_archive SET pinned=1 WHERE record_id=?", (identity,)
                )

        await self._io(write)

    async def prune(self, as_of, *, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("prune requires a 1–1000 batch")
        at = utc_datetime(as_of)
        raw = _time(at - timedelta(days=self.retention.raw_days))
        minute = _time(at - timedelta(days=self.retention.minute_days))

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                expired = connection.execute(
                    "SELECT * FROM market_archive WHERE pinned=0 AND "
                    "((kind='raw' AND (event_seconds<? OR "
                    "(event_seconds=? AND event_microseconds<?))) OR "
                    "(kind='minute' AND (event_seconds<? OR "
                    "(event_seconds=? AND event_microseconds<?)))) "
                    "ORDER BY event_seconds,event_microseconds,record_id LIMIT ?",
                    (raw[0], raw[0], raw[1], minute[0], minute[0], minute[1], limit + 1),
                ).fetchall()
                batch = expired[:limit]
                for row in batch:
                    self._decode(row)
                connection.executemany(
                    "DELETE FROM market_archive WHERE record_id=? AND pinned=0",
                    ((row["record_id"],) for row in batch),
                )
                return RetentionReport(
                    raw_removed=sum(row["kind"] == "raw" for row in batch),
                    minute_removed=sum(row["kind"] == "minute" for row in batch),
                    has_more=len(expired) > limit,
                )

        return await self._io(write)

    async def backup(self, destination: Path):
        def write():
            from .backup import copy_database

            copy_database(self.path, destination, kind="market")

        await self._io(write)
