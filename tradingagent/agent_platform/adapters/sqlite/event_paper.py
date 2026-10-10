"""Event-owned Paper wallet; reuses the existing pure kernel and account projection."""

import json
from contextlib import closing

from agent_platform.adapters.paper.futures import PaperFuturesBackend
from agent_platform.application.shared_futures_risk import SharedFuturesRisk
from agent_platform.domain.agent_trade_evidence import (
    EventAgentTradeEvidence,
    GuardianTradeEvidence,
)
from agent_platform.domain.futures_paper import (
    FuturesPaperOrder,
    FuturesPaperRules,
    FuturesPaperSettings,
    FuturesPaperState,
)
from agent_platform.domain.futures_paper_engine import (
    create_account,
    execute,
    liquidate,
    settle_funding,
    value,
)
from agent_platform.domain.position_protection import protection_triggered
from agent_platform.domain.trade_authorization import FuturesRiskSettings
from agent_platform.domain.trading_execution import ExecutionReceipt, TradingAccountSnapshot
from agent_platform.domain.watches import fact_hash
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.trading_execution import ExecutionRejected

from .event_agent import load_lane, load_run
from .events import SqliteStore
from .position_protection import load_protection
from .watches import load_watch


def wallet(db, account_ref):
    row = db.execute(
        "SELECT body FROM event_paper_wallets WHERE account_ref=?", (account_ref,)
    ).fetchone()
    if row is None:
        raise ValueError("event_wallet_not_configured")
    return FuturesPaperState.model_validate_json(row[0])


def save_wallet(db, state):
    db.execute(
        "UPDATE event_paper_wallets SET revision=?,body=? WHERE account_ref=?",
        (state.revision, state.model_dump_json(), state.account_ref),
    )


def snapshot(state, scope, quote, at):
    valuation = value(state, quote, at)
    return TradingAccountSnapshot(
        scope=scope,
        revision=state.revision,
        status=state.status,
        free_usdt=state.free_usdt,
        margin_usdt=state.margin_usdt,
        quantity=state.quantity,
        side=state.side,
        entry_notional=state.entry_notional,
        realized_pnl_usdt=state.realized_pnl_usdt,
        funding_usdt=state.funding_usdt,
        fees_usdt=state.fees_usdt,
        equity_usdt=valuation.equity_usdt,
        unrealized_pnl_usdt=valuation.unrealized_pnl_usdt,
        quote=quote,
        captured_at=at,
        leverage=state.settings.leverage,
    )


class SqliteEventPaperBackend(SqliteStore, PaperFuturesBackend):
    def __init__(self, path, *, market_source="offline_replay"):
        SqliteStore.__init__(self, path)
        PaperFuturesBackend.__init__(self, self, market_source=market_source)

    async def configure(self, lane, at):
        if not lane.risk_configured:
            raise ValueError("risk_not_configured")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                persisted = load_lane(db, lane.lane_id)
                if persisted != lane:
                    raise ValueError("lane_changed")
                if db.execute(
                    "SELECT 1 FROM event_paper_wallets WHERE account_ref=?",
                    (lane.scope.account_ref,),
                ).fetchone():
                    return wallet(db, lane.scope.account_ref)
                state = create_account(
                    lane.session_id,
                    lane.symbol,
                    FuturesPaperSettings.model_validate(lane.limits.model_dump()),
                    FuturesPaperRules(
                        qty_step=lane.qty_step,
                        min_qty=lane.min_qty,
                        max_qty=lane.max_qty,
                        min_notional=lane.min_notional,
                        tick_size="0.01",
                        maintenance_margin_rate="0.005",
                        liquidation_fee_bps="50",
                    ),
                    at,
                )
                state = FuturesPaperState.model_validate(
                    state.model_dump()
                    | {
                        "status": "running" if lane.enabled else "paused",
                        "revision": 2 if lane.enabled else 1,
                    }
                )
                db.execute(
                    "INSERT INTO event_paper_wallets VALUES(?,?,?)",
                    (state.account_ref, state.revision, state.model_dump_json()),
                )
                return state

        return await self._io(write)

    async def get(self, account_ref):
        def read():
            with closing(self._connect()) as db:
                return wallet(db, account_ref)

        return await self._io(read)

    async def account(self, scope, at):
        return await PaperFuturesBackend.account(self, scope, at)

    async def account_at_quote(self, scope, quote, at):
        def read():
            with closing(self._connect()) as db:
                return snapshot(wallet(db, scope.account_ref), scope, quote, at)

        return await self._io(read)

    async def set_enabled(self, lane, at):
        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if load_lane(db, lane.lane_id) != lane:
                    raise ValueError("lane_changed")
                state = wallet(db, lane.scope.account_ref)
                if state.status == "liquidated":
                    return
                save_wallet(
                    db,
                    FuturesPaperState.model_validate(
                        state.model_dump()
                        | {
                            "status": "running" if lane.enabled else "paused",
                            "revision": state.revision + 1,
                            "updated_at": at,
                        }
                    ),
                )

        await self._io(write)

    @staticmethod
    def _archive(db, key, state, before, operation, quote=None, receipt=None, fingerprint=None):
        previous = db.execute(
            (
                "SELECT body FROM event_paper_operations WHERE account_ref=? ORDER BY "
                "sequence DESC LIMIT 1"
            ),
            (state.account_ref,),
        ).fetchone()
        prev = json.loads(previous[0])["content_hash"] if previous else "0" * 64
        body = {
            "command_id": key,
            "before_state": before.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
            "operation": operation.model_dump(mode="json"),
            "quote": quote.model_dump(mode="json") if quote else None,
            "receipt": receipt.model_dump(mode="json") if receipt else None,
            "previous_hash": prev,
        }
        body["content_hash"] = fact_hash(body)
        db.execute(
            (
                "INSERT INTO "
                "event_paper_operations(command_id,account_ref,fingerprint,body) "
                "VALUES(?,?,?,?)"
            ),
            (
                key,
                state.account_ref,
                fingerprint or fact_hash(body),
                json.dumps(body, separators=(",", ":")),
            ),
        )

    async def lookup(self, command, at):
        def read():
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT fingerprint,body FROM event_paper_operations WHERE command_id=?",
                    (command.command_id,),
                ).fetchone()
                if row is None:
                    return None
                if row["fingerprint"] != fact_hash(command):
                    raise EventIdentityConflict("command_identity_conflict")
                data = json.loads(row["body"])
                receipt = ExecutionReceipt.model_validate_json(json.dumps(data["receipt"]))
                return receipt.model_copy(update={"observed_at": at})

        return await self._io(read)

    async def submit(self, command, quote, at):
        self._scope(command.scope)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute(
                    "SELECT fingerprint,body FROM event_paper_operations WHERE command_id=?",
                    (command.command_id,),
                ).fetchone()
                if prior:
                    if prior[0] != fact_hash(command):
                        raise EventIdentityConflict("command_identity_conflict")
                    return ExecutionReceipt.model_validate_json(
                        json.dumps(json.loads(prior[1])["receipt"])
                    )
                e = command.decision_evidence
                if not isinstance(e, (EventAgentTradeEvidence, GuardianTradeEvidence)):
                    raise ExecutionRejected("event_provenance_required")
                lane = load_lane(db, e.lane_id)
                before = wallet(db, command.scope.account_ref)
                if (
                    lane.scope != command.scope
                    or before.revision != command.expected_account_revision
                    or quote.source != self.market_source
                    or not command.created_at <= at <= command.expires_at
                ):
                    raise ExecutionRejected("current_scope_or_revision")
                if isinstance(e, EventAgentTradeEvidence):
                    run = load_run(db, e.run_id)
                    watch = load_watch(db, e.watch_id)
                    d = watch.definition
                    partition = f"{d.market}:{d.symbol}:{d.interval}:{d.price_kind}"
                    if (
                        db.execute(
                            "SELECT 1 FROM watch_partitions WHERE partition_key=?", (partition,)
                        ).fetchone()
                        or not lane.enabled
                        or run.lane != lane
                        or run.status != "RUNNING"
                        or at >= run.deadline
                        or at >= run.event.expires_at
                        or watch.state != "TRIGGERED"
                        or watch.definition.version != e.definition_revision
                    ):
                        raise ExecutionRejected("event_retired")
                    SharedFuturesRisk.evaluate(
                        FuturesRiskSettings.from_lane(lane),
                        e.original_quote,
                        quote,
                        snapshot(before, command.scope, quote, at),
                        command.action,
                        command.quantity,
                        command.target_leverage,
                    )
                else:
                    protection = load_protection(db, e.protection_id)
                    if (
                        command.action != "reduce"
                        or protection.intent.scope != command.scope
                        or command.quantity > min(protection.filled_quantity, before.quantity)
                        or not protection_triggered(
                            protection, lane, snapshot(before, command.scope, quote, at), quote
                        )
                    ):
                        raise ExecutionRejected("protection_scope_or_condition")
                state = before
                if isinstance(e, GuardianTradeEvidence) and state.status == "paused":
                    state = state.model_copy(update={"status": "running"})
                transition = execute(
                    state,
                    FuturesPaperOrder(
                        action=command.action,
                        quantity=command.quantity,
                        target_leverage=command.target_leverage,
                    ),
                    quote,
                    at,
                )
                after = transition.state
                if before.status == "paused" and after.status != "liquidated":
                    after = after.model_copy(update={"status": "paused"})
                receipt = self._receipt(command, transition.operation, at)
                save_wallet(db, after)
                self._archive(
                    db,
                    command.command_id,
                    after,
                    before,
                    transition.operation,
                    quote,
                    receipt,
                    fact_hash(command),
                )
                return receipt

        return await self._io(write)

    async def mark(self, scope, quote, at):
        if quote.source != self.market_source:
            raise ExecutionRejected("market_source")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                before = wallet(db, scope.account_ref)
                valuation = value(before, quote, at)
                if valuation.requires_liquidation:
                    transition = liquidate(before, quote.mark, at, quote=quote)
                    save_wallet(db, transition.state)
                    self._archive(
                        db,
                        f"liquidation:{scope.account_ref}:{before.revision}",
                        transition.state,
                        before,
                        transition.operation,
                        quote,
                    )
                else:
                    save_wallet(db, before.model_copy(update={"last_quote": quote}))

        await self._io(write)

    async def settle(self, scope, funding, at):
        if funding.source != self.market_source:
            raise ExecutionRejected("funding_source")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                before = wallet(db, scope.account_ref)
                if before.last_funding_at and funding.settled_at <= before.last_funding_at:
                    return
                transition = settle_funding(before, funding, at)
                save_wallet(db, transition.state)
                self._archive(
                    db,
                    f"funding:{scope.account_ref}:{funding.settled_at.isoformat()}",
                    transition.state,
                    before,
                    transition.operation,
                )

        await self._io(write)

    async def recent(self, scope):
        def read():
            with closing(self._connect()) as db:
                rows = db.execute(
                    (
                        "SELECT body FROM event_paper_operations WHERE account_ref=? ORDER BY "
                        "sequence DESC LIMIT 100"
                    ),
                    (scope.account_ref,),
                ).fetchall()
                return tuple(json.loads(r[0]) for r in rows)

        return await self._io(read)
