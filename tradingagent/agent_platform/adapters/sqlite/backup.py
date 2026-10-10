"""SQLite online backup from read-only source; existing targets are never replaced."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from agent_platform.adapters.owned_io import owned_thread
from agent_platform.ports.sessions import PersistenceUnavailable


def _validate_kind(connection, kind):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in ((7, 8, 9, 10) if kind == "core" else (1,)):
        raise ValueError("backup source has an unsupported schema")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
        if not row[0].startswith("sqlite_")
    }
    if kind == "market":
        from .retention import _TABLE

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='market_archive'"
        ).fetchone()
        if tables != {"market_archive"} or row is None or row[0] != _TABLE:
            raise ValueError("backup source has an unsupported archive schema")
    else:
        from .migrations import _REQUIRED_COLUMNS, _validate_schema

        _validate_schema(connection, version)
        expected = {table for minimum, table, _ in _REQUIRED_COLUMNS if version >= minimum}
        if tables != expected:
            raise ValueError("backup source has an unsupported core schema")
    return version


def copy_database(source: Path, destination: Path, *, kind: str):
    source, target = Path(source).resolve(), Path(destination).resolve()
    if source == target or kind not in ("core", "market"):
        raise ValueError("backup needs distinct paths and a supported database kind")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as reader:
        # Pin one read snapshot: precheck, copy and resulting report refer to the same schema.
        reader.execute("BEGIN")
        version = _validate_kind(reader, kind)
        with target.open("xb"):
            pass
        deadline = monotonic() + 60

        def progress(status, remaining, total):
            if monotonic() >= deadline:
                raise PersistenceUnavailable("online backup exceeded its time bound")

        with closing(sqlite3.connect(target)) as copy:
            reader.backup(copy, pages=256, sleep=0.01, progress=progress)
            _validate_kind(copy, kind)
            if (
                copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or copy.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise PersistenceUnavailable("online backup verification failed")
            count = copy.execute(
                "SELECT count(*) FROM " + ("journal_events" if kind == "core" else "market_archive")
            ).fetchone()[0]
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return dict(
        schema_version=1,
        kind=kind,
        source_schema_version=version,
        integrity="ok",
        sha256=digest.hexdigest(),
        bytes=target.stat().st_size,
        **{("journal_events" if kind == "core" else "market_records"): count},
    )


async def backup_database(source: Path, destination: Path, *, kind: str):
    try:
        return await owned_thread(lambda: copy_database(source, destination, kind=kind))
    except FileExistsError:
        raise FileExistsError("backup target already exists") from None
    except (OSError, sqlite3.Error):
        raise PersistenceUnavailable("online backup unavailable") from None
