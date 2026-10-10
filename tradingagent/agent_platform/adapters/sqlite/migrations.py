"""Upgrade compatible databases atomically after creating a SQLite-aware backup."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from agent_platform.ports.sessions import PersistenceUnavailable

from .quote_evidence import backfill_quotes
from .schema import (
    BUDGET_SCHEMA,
    DECISION_SCHEMA,
    EVENT_AGENT_SCHEMA,
    EVENT_PAPER_SCHEMA,
    JOURNAL_SCHEMA,
    OBSERVATION_SCHEMA,
    QUOTE_EVIDENCE_SCHEMA,
    SCHEMA_VERSION,
    SESSION_SCHEMA,
    STATE_SCHEMA,
    WATCH_SCHEMA,
)

_REQUIRED_COLUMNS = (
    (10, "agent_trade_intents", {"intent_id", "lane_id", "account_ref", "body"}),
    (10, "position_protections", {"protection_id", "account_ref", "revision", "status", "body"}),
    (10, "event_paper_wallets", {"account_ref", "revision", "body"}),
    (
        10,
        "event_paper_operations",
        {"sequence", "command_id", "account_ref", "fingerprint", "body"},
    ),
    (10, "execution_commands", {"command_id", "account_ref", "revision", "status", "body"}),
    (10, "execution_updates", {"sequence", "command_id", "body"}),
    (9, "event_agent_lanes", {"lane_id", "account_ref", "revision", "body"}),
    (9, "agent_runs", {"run_id", "request_key", "lane_id", "status", "body"}),
    (9, "agent_turns", {"request_id", "run_id", "status", "body"}),
    (9, "agent_tool_calls", {"run_id", "tool_call_id", "body"}),
    (9, "agent_budget_links", {"request_id", "lane_id", "grant_id"}),
    (8, "watches", {"watch_id", "lane_id", "revision", "state", "body"}),
    (8, "watch_definitions", {"watch_id", "version", "body"}),
    (8, "watch_inputs", {"partition_key", "candle_key", "content_hash"}),
    (8, "watch_partitions", {"partition_key", "conflicted"}),
    (8, "watch_events", {"sequence", "event_id", "watch_id", "lane_id", "body"}),
    (8, "agent_event_deliveries", {"event_id", "lane_id", "status", "body"}),
    (1, "sessions", {"session_id", "revision", "status", "active_slot", "body"}),
    (1, "session_events", {"sequence", "event_id", "session_id", "revision", "body"}),
    (2, "journal_events", {"sequence", "event_id", "aggregate_id", "kind", "occurred_at", "body"}),
    (
        3,
        "budget_requests",
        {"reservation_id", "request_id", "budget_day", "requested_at", "body", "usage_body"},
    ),
    (3, "budget_settings", {"singleton", "billing_frozen"}),
    (4, "domain_states", {"key", "revision", "state_type", "body", "event_id"}),
    (4, "state_commits", {"event_id"}),
    (5, "account_snapshots", {"account_ref", "market_type", "body"}),
    (
        5,
        "observed_trades",
        {"sequence", "venue", "market_type", "account_ref", "symbol", "trade_id", "body"},
    ),
    (5, "trade_cursors", {"account_ref", "market_type", "symbol", "body", "trade_sequence"}),
    (5, "import_results", {"event_id", "account_input", "body"}),
    (6, "trade_quote_evidence", {"trade_sequence", "quote_quantity", "source_event_id"}),
    (
        7,
        "decision_requests",
        {
            "request_id",
            "body",
            "claim_event_id",
            "completion_body",
            "result_body",
            "completion_event_id",
        },
    ),
)


_ALL_STATEMENTS = (
    *SESSION_SCHEMA,
    *JOURNAL_SCHEMA,
    *BUDGET_SCHEMA,
    *STATE_SCHEMA,
    *OBSERVATION_SCHEMA,
    *QUOTE_EVIDENCE_SCHEMA,
    *DECISION_SCHEMA,
    *WATCH_SCHEMA,
    *EVENT_AGENT_SCHEMA,
    *EVENT_PAPER_SCHEMA,
)


def _canonical_sql(statement: str) -> str:
    return re.sub(r"\s+", "", statement.lower().replace("if not exists", "")).rstrip(";")


def _validate_constraints(
    connection: sqlite3.Connection, reference: sqlite3.Connection, table: str
) -> None:
    if table in (
        "trade_quote_evidence",
        "decision_requests",
        "watches",
        "watch_definitions",
        "watch_inputs",
        "watch_partitions",
        "watch_events",
        "agent_event_deliveries",
        "event_agent_lanes",
        "agent_runs",
        "agent_turns",
        "agent_tool_calls",
        "agent_budget_links",
        "agent_trade_intents",
        "position_protections",
        "event_paper_wallets",
        "event_paper_operations",
    ):
        query = "SELECT sql FROM sqlite_master WHERE type='table' AND name=?"
        actual_sql = connection.execute(query, (table,)).fetchone()[0]
        expected_sql = reference.execute(query, (table,)).fetchone()[0]
        if _canonical_sql(actual_sql) != _canonical_sql(expected_sql):
            raise PersistenceUnavailable("local transaction constraints are incompatible")
    actual = {row[1]: row for row in connection.execute(f"PRAGMA table_info({table})")}
    expected = {row[1]: row for row in reference.execute(f"PRAGMA table_info({table})")}
    if any(
        column not in actual or actual[column][2:4] != row[2:4] or actual[column][5] != row[5]
        for column, row in expected.items()
    ):
        raise PersistenceUnavailable("local database constraints are incompatible")

    def unique_keys(database):
        return {
            tuple(part[2] for part in database.execute(f'PRAGMA index_info("{index[1]}")'))
            for index in database.execute(f"PRAGMA index_list({table})")
            if index[2] and not index[4]
        }

    if not unique_keys(reference) <= unique_keys(connection):
        raise PersistenceUnavailable("local database uniqueness constraints are incompatible")
    expected_fks = {
        tuple(row[2:]) for row in reference.execute(f"PRAGMA foreign_key_list({table})")
    }
    actual_fks = {tuple(row[2:]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
    if not expected_fks <= actual_fks:
        raise PersistenceUnavailable("local database references are incompatible")
    indexes = reference.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    for name, statement in indexes:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        if row is None or _canonical_sql(row[0]) != _canonical_sql(statement):
            raise PersistenceUnavailable("local database indexes are incompatible")


def _validate_schema(connection: sqlite3.Connection, version: int) -> None:
    if version == 0:
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        if any(not row[0].startswith("sqlite_") for row in objects):
            raise PersistenceUnavailable("unversioned local database is incompatible")
    with closing(sqlite3.connect(":memory:")) as reference:
        for statement in _ALL_STATEMENTS:
            reference.execute(statement)
        for minimum_version, table, required in _REQUIRED_COLUMNS:
            if version >= minimum_version:
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    raise PersistenceUnavailable("local database schema is incompatible")
                _validate_constraints(connection, reference, table)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise PersistenceUnavailable("local database reference integrity check failed")


def _backup_database(path: Path) -> None:
    # A separate reader can copy the last committed WAL snapshot while the
    # migration connection holds BEGIN IMMEDIATE and excludes other writers.
    backup_path = path.with_name(f"{path.name}.pre-v{SCHEMA_VERSION}-{uuid4().hex}.bak")
    with closing(sqlite3.connect(path)) as reader, closing(sqlite3.connect(backup_path)) as backup:
        reader.backup(backup)


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SCHEMA_VERSION:
            raise PersistenceUnavailable("unsupported local database version")
        _validate_schema(connection, version)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PersistenceUnavailable("local database integrity check failed")
        connection.execute("PRAGMA journal_mode = WAL")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            # Another initializer may have completed while this one was backing up.
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= current <= SCHEMA_VERSION:
                raise PersistenceUnavailable("unsupported local database version")
            _validate_schema(connection, current)
            if 0 < current < SCHEMA_VERSION:
                _backup_database(path)
            for statement in _ALL_STATEMENTS:
                connection.execute(statement)
            if 0 < current < 6:
                try:
                    backfill_quotes(connection)
                except ValueError:
                    raise PersistenceUnavailable(
                        "historical trade quote evidence is incompatible"
                    ) from None
            _validate_schema(connection, SCHEMA_VERSION)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
