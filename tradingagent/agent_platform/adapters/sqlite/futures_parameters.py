"""Explicit, audited upgrade of a paused flat wallet; never creates funds."""

from contextlib import closing

from agent_platform.domain.common import utc_datetime
from agent_platform.domain.futures_paper import FuturesPaperSettings, FuturesPaperState
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy, TradingRun
from agent_platform.ports.futures_paper import FuturesPaperGuardConflict

from .futures_paper import SqliteFuturesPaperStore


class SqliteFuturesParameterMigration(SqliteFuturesPaperStore):
    async def enable(self, account_ref, expected_revision, at, *, style_revision, leverage_choices):
        at = utc_datetime(at)
        # Reuse strict candidate validation before reading or writing any state.
        selection = TradingPolicy(
            order_notional_usdt="1",
            max_price_drift_bps="0",
            min_confidence="1",
            strategy_instructions="migration",
            leverage_choices=leverage_choices,
        )

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                account = self._account(db, account_ref)
                self._expected(account, expected_revision)
                self._session(db, account.session_id, at, style=style_revision)
                if account.status != "paused" or account.quantity or at < account.updated_at:
                    raise FuturesPaperGuardConflict(
                        "parameter migration requires a paused flat wallet"
                    )
                row = db.execute(
                    "SELECT body FROM futures_trading_runs WHERE account_ref=?", (account_ref,)
                ).fetchone()
                if row is None:
                    raise FuturesPaperGuardConflict("parameter migration requires an existing run")
                run = TradingRun.model_validate_json(row[0])
                if (
                    run.scope.session_id != account.session_id
                    or run.scope.symbol != account.symbol
                    or run.scope.environment != "paper"
                ):
                    raise FuturesPaperGuardConflict("migration run identity conflicts")
                if (
                    db.execute(
                        "SELECT 1 FROM futures_trading_cycles "
                        "WHERE account_ref=? AND status='pending'",
                        (account_ref,),
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM execution_commands WHERE account_ref=? "
                        "AND status NOT IN ('filled','rejected','canceled')",
                        (account_ref,),
                    ).fetchone()
                ):
                    raise FuturesPaperGuardConflict(
                        "pending work must be resolved before migration"
                    )
                ceiling = max(max(selection.leverage_choices), account.settings.leverage)
                if run.policy.decision_mode == "parameterized":
                    if (
                        run.policy.leverage_choices == selection.leverage_choices
                        and run.limits.max_leverage == ceiling
                    ):
                        return account
                    raise FuturesPaperGuardConflict("existing parameter policy cannot be replaced")
                policy = TradingPolicy.model_validate(
                    run.policy.model_dump()
                    | {
                        "decision_mode": "parameterized",
                        "leverage_choices": selection.leverage_choices,
                    }
                )
                limits = TradingLimits.model_validate(
                    run.limits.model_dump() | {"max_leverage": ceiling}
                )
                updated_run = TradingRun.model_validate(
                    run.model_dump() | {"policy": policy, "limits": limits}
                )
                updated = FuturesPaperState.model_validate(
                    account.model_dump()
                    | {
                        "settings": FuturesPaperSettings.model_validate(
                            account.settings.model_dump() | {"max_leverage": ceiling}
                        ),
                        "revision": account.revision + 1,
                        "updated_at": at,
                    }
                )
                record = self._commit(db, updated, "parameterize")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS futures_policy_migrations "
                    "(command_id TEXT PRIMARY KEY, account_ref TEXT NOT NULL, "
                    "old_body TEXT NOT NULL, new_body TEXT NOT NULL, changed_at TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO futures_policy_migrations VALUES(?,?,?,?,?)",
                    (
                        record.command_id,
                        account_ref,
                        run.model_dump_json(),
                        updated_run.model_dump_json(),
                        at.isoformat(),
                    ),
                )
                db.execute(
                    "UPDATE futures_trading_runs SET body=? WHERE account_ref=?",
                    (updated_run.model_dump_json(), account_ref),
                )
                return updated

        return await self._io(write)
