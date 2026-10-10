"""Immutable exchange facts and account/cursor projections commit with their audit."""

import sqlite3
from contextlib import closing

from agent_platform.domain.account import (
    AccountSnapshot,
    AccountSyncStatus,
    ObservedTrade,
    TradeBatch,
    TradeCursor,
)
from agent_platform.domain.common import MarketType, live_account_ref, required_identifier
from agent_platform.domain.events import AppendReceipt, ImportResult, JournalEvent, StateRecord
from agent_platform.domain.reviews import TradeAttribution
from agent_platform.ports.persistence import EventIdentityConflict, ObservationConflict

from .quote_evidence import record_quote
from .state import SqliteStateStore


class SqliteObservationStore(SqliteStateStore):
    @staticmethod
    def _account(
        connection: sqlite3.Connection, account_ref: str, market_type: MarketType
    ) -> AccountSnapshot | None:
        row = connection.execute(
            "SELECT body FROM account_snapshots WHERE account_ref=? AND market_type=?",
            (account_ref, market_type.value),
        ).fetchone()
        return AccountSnapshot.model_validate_json(row["body"]) if row else None

    async def account_snapshot(
        self, account_ref: str, market_type: MarketType = MarketType.SPOT
    ) -> AccountSnapshot | None:
        ref, market = live_account_ref(account_ref), MarketType(market_type)

        def read():
            with closing(self._connect()) as connection:
                return self._account(connection, ref, market)

        return await self._io(read)

    async def cursor(
        self, account_ref: str, symbol: str, market_type: MarketType = MarketType.SPOT
    ) -> TradeCursor:
        scope = (
            live_account_ref(account_ref),
            MarketType(market_type).value,
            required_identifier(symbol),
        )

        def read():
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT body FROM trade_cursors "
                    "WHERE account_ref=? AND market_type=? AND symbol=?",
                    scope,
                ).fetchone()
                return TradeCursor.model_validate_json(row["body"]) if row else TradeCursor()

        return await self._io(read)

    async def observed_trades(
        self, account_ref: str, symbol: str, market_type: MarketType = MarketType.SPOT
    ) -> tuple[ObservedTrade, ...]:
        scope = (
            live_account_ref(account_ref),
            MarketType(market_type).value,
            required_identifier(symbol),
        )

        def read():
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT body FROM observed_trades "
                    "WHERE account_ref=? AND market_type=? AND symbol=? "
                    "ORDER BY sequence",
                    scope,
                ).fetchall()
                facts = (ObservedTrade.model_validate_json(row["body"]) for row in rows)
                return tuple(sorted(facts, key=lambda item: item.executed_at))

        return await self._io(read)

    @staticmethod
    def _balances(snapshot: AccountSnapshot) -> dict:
        return {
            balance.asset: (balance.free, balance.locked)
            for balance in snapshot.balances
            if balance.total != 0
        }

    @classmethod
    def _save_account(
        cls, connection: sqlite3.Connection, snapshot: AccountSnapshot
    ) -> AccountSnapshot:
        previous = cls._account(connection, snapshot.account_ref, snapshot.market_type)
        if previous is not None and snapshot.as_of < previous.as_of:
            raise ObservationConflict("older account observations require reconciliation")
        revision = previous.account_revision if previous else 0
        if snapshot.status == AccountSyncStatus.FRESH:
            if revision == 0 or cls._balances(previous) != cls._balances(snapshot):
                revision += 1
            values = {**snapshot.model_dump(), "account_revision": revision}
        elif previous is not None:
            values = {**previous.model_dump(), "status": snapshot.status}
        else:
            values = {**snapshot.model_dump(), "balances": (), "account_revision": 0}
        current = AccountSnapshot.model_validate(values)
        connection.execute(
            "INSERT INTO account_snapshots(account_ref, market_type, body) VALUES(?,?,?) "
            "ON CONFLICT(account_ref, market_type) DO UPDATE SET body=excluded.body",
            (current.account_ref, current.market_type.value, current.model_dump_json()),
        )
        return current

    @classmethod
    def _save_trade(
        cls, connection: sqlite3.Connection, trade: ObservedTrade, occurred_at, event_id: str
    ) -> bool:
        scope = (
            trade.venue,
            trade.market_type.value,
            trade.account_ref,
            trade.symbol,
            trade.trade_id,
        )
        previous = connection.execute(
            "SELECT body, sequence FROM observed_trades WHERE venue=? AND market_type=? "
            "AND account_ref=? AND symbol=? AND trade_id=?",
            scope,
        ).fetchone()
        if previous is not None:
            known = ObservedTrade.model_validate_json(previous["body"])
            quote_conflict = (
                known.quote_quantity is not None
                and trade.quote_quantity is not None
                and known.quote_quantity != trade.quote_quantity
            )
            if quote_conflict or known.model_copy(
                update={"quote_quantity": None}
            ) != trade.model_copy(update={"quote_quantity": None}):
                raise ObservationConflict("trade identifier already refers to a different fact")
            # Absence is unknown, not a contradiction. Keep the original row;
            # the incoming audit can carry new evidence without rewriting history.
            record_quote(connection, previous["sequence"], trade, event_id)
            return False
        inserted = connection.execute(
            "INSERT INTO observed_trades(venue, market_type, account_ref, symbol, trade_id, body) "
            "VALUES(?,?,?,?,?,?)",
            (*scope, trade.model_dump_json()),
        )
        record_quote(connection, inserted.lastrowid, trade, event_id)
        attribution = TradeAttribution(
            venue=trade.venue,
            market_type=trade.market_type,
            account_ref=trade.account_ref,
            symbol=trade.symbol,
            trade_id=trade.trade_id,
        )
        existing = cls._load(connection, attribution.aggregate_id)
        if existing is not None:
            if (
                existing.state_type != "attribution"
                or existing.state.identity != attribution.identity
            ):
                raise ObservationConflict("attribution identity conflicts with the observed trade")
            return True
        event = JournalEvent(
            event_id="trade-unclassified:" + attribution.aggregate_id,
            aggregate_id=attribution.aggregate_id,
            kind="attribution_recorded",
            payload=attribution,
            occurred_at=occurred_at,
        )
        cls._append(connection, event)
        record = StateRecord(
            key=attribution.aggregate_id,
            revision=1,
            state_type="attribution",
            state=attribution,
            updated_at=occurred_at,
        )
        connection.execute(
            "INSERT INTO domain_states(key, revision, state_type, body, event_id) "
            "VALUES(?,?,?,?,?)",
            (
                record.key,
                record.revision,
                record.state_type.value,
                record.model_dump_json(),
                event.event_id,
            ),
        )
        return True

    @staticmethod
    def _save_cursor(connection: sqlite3.Connection, batch: TradeBatch) -> TradeCursor:
        scope = batch.account_ref, batch.market_type.value, batch.symbol
        old_row = connection.execute(
            "SELECT body, trade_sequence FROM trade_cursors "
            "WHERE account_ref=? AND market_type=? AND symbol=?",
            scope,
        ).fetchone()
        old = TradeCursor.model_validate_json(old_row["body"]) if old_row else TradeCursor()
        candidate = batch.next_cursor
        if candidate.last_trade_id is None:
            if candidate.last_executed_at is not None:
                raise ObservationConflict("time-only import cursor cannot skip unidentified facts")
            return old
        target = connection.execute(
            "SELECT sequence, body FROM observed_trades WHERE venue='binance' "
            "AND account_ref=? AND market_type=? AND symbol=? AND trade_id=?",
            (*scope, candidate.last_trade_id),
        ).fetchone()
        if target is None:
            raise ObservationConflict("import cursor does not reference a committed scoped trade")
        fact = ObservedTrade.model_validate_json(target["body"])
        if (
            candidate.last_executed_at is not None
            and candidate.last_executed_at != fact.executed_at
        ):
            raise ObservationConflict("cursor time does not match its trade")
        if old_row and (
            fact.executed_at < old.last_executed_at
            or (
                fact.executed_at == old.last_executed_at
                and target["sequence"] < old_row["trade_sequence"]
            )
        ):
            raise ObservationConflict("committed import cursor cannot regress")
        current = TradeCursor(last_trade_id=fact.trade_id, last_executed_at=fact.executed_at)
        connection.execute(
            "INSERT INTO trade_cursors(account_ref, market_type, symbol, body, trade_sequence) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_ref, market_type, symbol) "
            "DO UPDATE SET body=excluded.body, trade_sequence=excluded.trade_sequence",
            (*scope, current.model_dump_json(), target["sequence"]),
        )
        return current

    async def ingest(
        self, batch: TradeBatch, account: AccountSnapshot, event: JournalEvent
    ) -> ImportResult:
        facts = TradeBatch.model_validate_json(batch.model_dump_json())
        snapshot = AccountSnapshot.model_validate_json(account.model_dump_json())
        audit = JournalEvent.model_validate_json(event.model_dump_json())
        if (
            facts.account_ref != snapshot.account_ref
            or facts.market_type != snapshot.market_type
            or audit.kind != "trades_imported"
            or audit.payload != facts
            or audit.occurred_at < snapshot.as_of
            or any(trade.executed_at > audit.occurred_at for trade in facts.trades)
        ):
            raise ValueError("import account, scope, facts and audit must agree")
        times = tuple(trade.executed_at for trade in facts.trades)
        if times != tuple(sorted(times)):
            raise ValueError("imported facts must be chronological")

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT e.body AS event_body, r.body, r.account_input "
                    "FROM journal_events e LEFT JOIN import_results r USING(event_id) "
                    "WHERE e.event_id=?",
                    (audit.event_id,),
                ).fetchone()
                if previous is not None:
                    if (
                        JournalEvent.model_validate_json(previous["event_body"]) != audit
                        or previous["body"] is None
                        or AccountSnapshot.model_validate_json(previous["account_input"])
                        != snapshot
                    ):
                        raise EventIdentityConflict(
                            "import event already refers to different inputs"
                        )
                    original = ImportResult.model_validate_json(previous["body"])
                    return ImportResult.model_validate(
                        {
                            **original.model_dump(),
                            "imported_count": 0,
                            "duplicate_count": len(facts.trades),
                            "receipt": AppendReceipt(
                                event_id=original.receipt.event_id,
                                sequence=original.receipt.sequence,
                                appended=False,
                            ),
                        }
                    )
                current = self._save_account(connection, snapshot)
                self._append(
                    connection,
                    JournalEvent(
                        event_id="account-observed:" + audit.event_id,
                        aggregate_id=current.account_ref,
                        kind="account_observed",
                        payload=current,
                        occurred_at=audit.occurred_at,
                    ),
                )
                imported = sum(
                    self._save_trade(connection, trade, audit.occurred_at, audit.event_id)
                    for trade in facts.trades
                )
                cursor = self._save_cursor(connection, facts)
                receipt = self._append(connection, audit)
                result = ImportResult(
                    account_ref=facts.account_ref,
                    symbol=facts.symbol,
                    imported_count=imported,
                    duplicate_count=len(facts.trades) - imported,
                    next_cursor=cursor,
                    account_revision=current.account_revision,
                    account=current,
                    receipt=receipt,
                )
                connection.execute(
                    "INSERT INTO import_results(event_id, account_input, body) VALUES(?,?,?)",
                    (audit.event_id, snapshot.model_dump_json(), result.model_dump_json()),
                )
                return result

        return await self._io(write)
