# Review package: Task 1–3 Event Watch foundation

Repository: D:/develop/tradingagent/Blockchain-Trading-Lab/tradingagent
Branch: codex/event-watch-core. Source baseline is mostly untracked: review these workspace files, not an empty Git diff. No commits or user services were changed.

Plan: docs/superpowers/plans/2026-10-09-event-driven-agent-mvp.md
Spec: docs/superpowers/specs/2026-10-09-event-driven-agent-design.md
Ledger: docs/superpowers/implementation/2026-10-09-event-watch-progress.md

New code:
- domain/watches.py, watch_features.py, watch_rules.py, agent_events.py
- ports/watches.py, agent_events.py, watch_data.py
- adapters/sqlite/watches.py, agent_events.py
- adapters/binance_direct/watch_data.py
- application/watches.py, runtime/watches.py, replay/watches.py

Modified existing code: adapters/sqlite/schema.py, migrations.py, backup.py only.
New tests: tests/domain/test_watch_rules.py, test_watch_features.py; tests/fixtures/watch_cases.py; tests/adapters/test_watch_store.py, test_agent_event_delivery.py, test_watch_data.py; tests/runtime/test_watch_runtime.py; tests/replay/test_watch_replay.py; tests/architecture/test_watch_boundaries.py.
Legacy tests modified only for schema-version assertions: test_sqlite_decisions.py, test_operations.py, test_retention.py.

Current validation: 116 tests pass in integrated isolated offline run. Aggregate architecture checker has existing violations in futures_trading.py and jev_tasks.py; new Watch boundary test passes. No model/network/account/order activation.

Review all Task 1–3 behavior including missing-data tri-state, TTL, cancellation/replacement races, cursor ordering/restart, same-bar repair, durable partition conflict, SQLite rollback/migration/backup, event lease recovery, REST/WS market identity, closed native interval boundaries, runtime isolation/cancellation, and replay identity/final-state equality. Task 4–9 are outside implementation scope.
