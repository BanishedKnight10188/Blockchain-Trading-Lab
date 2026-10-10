"""Ordered schema versions shared by all adapters using the same database."""

SCHEMA_VERSION = 10

EVENT_AGENT_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS event_agent_lanes (
        lane_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL UNIQUE,
        revision INTEGER NOT NULL CHECK(revision>0), body TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
        lane_id TEXT NOT NULL REFERENCES event_agent_lanes(lane_id),
        status TEXT NOT NULL, body TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_active_agent_run
       ON agent_runs(lane_id) WHERE status IN ('RUNNING','RECONCILING')""",
    """CREATE TABLE IF NOT EXISTS agent_turns (
        request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
        status TEXT NOT NULL, body TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_tool_calls (
        run_id TEXT NOT NULL REFERENCES agent_runs(run_id), tool_call_id TEXT NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(run_id,tool_call_id)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_budget_links (
        request_id TEXT PRIMARY KEY REFERENCES budget_requests(request_id),
        lane_id TEXT NOT NULL, grant_id TEXT NOT NULL
    )""",
)

WATCH_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS watches (
        watch_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        state TEXT NOT NULL CHECK(state IN
                              ('ARMED','TRIGGERED','EXPIRED','INVALIDATED','CANCELLED')),
        body TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS watches_lane_state ON watches(lane_id,state)""",
    """CREATE TABLE IF NOT EXISTS watch_definitions (
        watch_id TEXT NOT NULL REFERENCES watches(watch_id),
        version INTEGER NOT NULL CHECK(version > 0), body TEXT NOT NULL,
        PRIMARY KEY(watch_id,version)
    )""",
    """CREATE TABLE IF NOT EXISTS watch_inputs (
        partition_key TEXT NOT NULL, candle_key TEXT NOT NULL, content_hash TEXT NOT NULL,
        PRIMARY KEY(partition_key,candle_key)
    )""",
    """CREATE TABLE IF NOT EXISTS watch_partitions (
        partition_key TEXT PRIMARY KEY, conflicted INTEGER NOT NULL CHECK(conflicted=1)
    )""",
    """CREATE TABLE IF NOT EXISTS watch_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        watch_id TEXT NOT NULL REFERENCES watches(watch_id),
        lane_id TEXT NOT NULL, body TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_event_deliveries (
        event_id TEXT PRIMARY KEY REFERENCES watch_events(event_id),
        lane_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('QUEUED','LEASED','DISPATCHED','RECONCILING',
                                             'COMPLETED','FAILED','SUPPRESSED')),
        body TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS agent_deliveries_lane_status
        ON agent_event_deliveries(lane_id,status)""",
)

SESSION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL CHECK(revision > 0),
        status TEXT NOT NULL,
        active_slot INTEGER NOT NULL DEFAULT 1 CHECK(active_slot = 1),
        body TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_open_session
        ON sessions(active_slot) WHERE status <> 'closed'""",
    """CREATE TABLE IF NOT EXISTS session_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES sessions(session_id),
        revision INTEGER NOT NULL,
        body TEXT NOT NULL,
        UNIQUE(session_id, revision)
    )""",
)

JOURNAL_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS journal_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        aggregate_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        body TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS journal_aggregate_sequence
        ON journal_events(aggregate_id, sequence)""",
)

BUDGET_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS budget_requests (
        reservation_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        budget_day TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        body TEXT NOT NULL,
        usage_body TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS budget_day_requests
        ON budget_requests(budget_day)""",
    """CREATE INDEX IF NOT EXISTS budget_hour_requests
        ON budget_requests(requested_at)""",
    """CREATE TABLE IF NOT EXISTS budget_settings (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        billing_frozen INTEGER NOT NULL DEFAULT 0 CHECK(billing_frozen IN (0,1))
    )""",
    """INSERT OR IGNORE INTO budget_settings(singleton, billing_frozen) VALUES(1,0)""",
)

STATE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS state_commits (
        event_id TEXT PRIMARY KEY REFERENCES journal_events(event_id)
    )""",
    """CREATE TABLE IF NOT EXISTS domain_states (
        key TEXT PRIMARY KEY,
        revision INTEGER NOT NULL CHECK(revision>0),
        state_type TEXT NOT NULL CHECK(state_type<>'session'),
        body TEXT NOT NULL,
        event_id TEXT NOT NULL REFERENCES journal_events(event_id)
    )""",
    """CREATE TRIGGER IF NOT EXISTS session_state_key_collision BEFORE INSERT ON sessions
        WHEN EXISTS(SELECT 1 FROM domain_states WHERE key=NEW.session_id)
        BEGIN SELECT RAISE(ABORT, 'state key conflict'); END""",
    """CREATE TRIGGER IF NOT EXISTS domain_state_key_collision BEFORE INSERT ON domain_states
        WHEN EXISTS(SELECT 1 FROM sessions WHERE session_id=NEW.key)
        BEGIN SELECT RAISE(ABORT, 'state key conflict'); END""",
)

OBSERVATION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS account_snapshots (
        account_ref TEXT NOT NULL, market_type TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(account_ref, market_type)
    )""",
    """CREATE TABLE IF NOT EXISTS observed_trades (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        venue TEXT NOT NULL, market_type TEXT NOT NULL, account_ref TEXT NOT NULL,
        symbol TEXT NOT NULL, trade_id TEXT NOT NULL, body TEXT NOT NULL,
        UNIQUE(venue, market_type, account_ref, symbol, trade_id)
    )""",
    """CREATE TABLE IF NOT EXISTS trade_cursors (
        account_ref TEXT NOT NULL, market_type TEXT NOT NULL, symbol TEXT NOT NULL,
        body TEXT NOT NULL, trade_sequence INTEGER NOT NULL REFERENCES observed_trades(sequence),
        PRIMARY KEY(account_ref, market_type, symbol)
    )""",
    """CREATE TABLE IF NOT EXISTS import_results (
        event_id TEXT PRIMARY KEY REFERENCES journal_events(event_id),
        account_input TEXT NOT NULL, body TEXT NOT NULL
    )""",
)

QUOTE_EVIDENCE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS trade_quote_evidence (
        trade_sequence INTEGER PRIMARY KEY REFERENCES observed_trades(sequence),
        quote_quantity TEXT NOT NULL,
        source_event_id TEXT NOT NULL REFERENCES journal_events(event_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
)

DECISION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS decision_requests (
        request_id TEXT PRIMARY KEY,
        body TEXT NOT NULL,
        claim_event_id TEXT NOT NULL REFERENCES journal_events(event_id),
        completion_body TEXT,
        result_body TEXT,
        completion_event_id TEXT REFERENCES journal_events(event_id),
        CHECK((completion_body IS NULL AND result_body IS NULL AND completion_event_id IS NULL)
            OR (completion_body IS NOT NULL AND result_body IS NOT NULL
                AND completion_event_id IS NOT NULL))
    )""",
)

EVENT_PAPER_SCHEMA = (
    (
        "CREATE TABLE IF NOT EXISTS agent_trade_intents (intent_id TEXT "
        "PRIMARY KEY, lane_id TEXT NOT NULL REFERENCES "
        "event_agent_lanes(lane_id), account_ref TEXT NOT NULL, body TEXT NOT "
        "NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS position_protections (protection_id TEXT "
        "PRIMARY KEY REFERENCES agent_trade_intents(intent_id), account_ref "
        "TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), status "
        "TEXT NOT NULL CHECK(status IN "
        "('prepared','ready','closing','closed','degraded')), body TEXT NOT "
        "NULL)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS position_protections_account ON "
        "position_protections(account_ref,status)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS event_paper_wallets (account_ref TEXT "
        "PRIMARY KEY REFERENCES event_agent_lanes(account_ref), revision "
        "INTEGER NOT NULL CHECK(revision>0), body TEXT NOT NULL)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS event_paper_operations (sequence INTEGER "
        "PRIMARY KEY AUTOINCREMENT, command_id TEXT UNIQUE NOT NULL, "
        "account_ref TEXT NOT NULL REFERENCES "
        "event_paper_wallets(account_ref), fingerprint TEXT NOT NULL, body "
        "TEXT NOT NULL)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS event_paper_operations_account ON "
        "event_paper_operations(account_ref,sequence)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS execution_commands (command_id TEXT "
        "PRIMARY KEY, account_ref TEXT NOT NULL, revision INTEGER NOT NULL "
        "CHECK(revision>0), status TEXT NOT NULL, body TEXT NOT NULL)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS execution_commands_account ON "
        "execution_commands(account_ref,status)"
    ),
    (
        "CREATE TABLE IF NOT EXISTS execution_updates (sequence INTEGER "
        "PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL REFERENCES "
        "execution_commands(command_id), body TEXT NOT NULL)"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS event_paper_operations_no_update BEFORE "
        "UPDATE ON event_paper_operations BEGIN SELECT RAISE(ABORT,'event "
        "paper archive is immutable'); END"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS event_paper_operations_no_delete BEFORE "
        "DELETE ON event_paper_operations BEGIN SELECT RAISE(ABORT,'event "
        "paper archive is immutable'); END"
    ),
)
