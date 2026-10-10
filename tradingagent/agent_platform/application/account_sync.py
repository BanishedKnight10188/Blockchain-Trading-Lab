"""Deterministic read-only polling with atomic facts, balances, cursor and audit."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from agent_platform.domain.account import (
    AccountSnapshot,
    AccountSyncStatus,
    PositionView,
    TradeBatch,
)
from agent_platform.domain.common import live_account_ref
from agent_platform.domain.events import JournalEvent
from agent_platform.domain.sync import SyncReport
from agent_platform.ports.account import AccountPort, AccountReadUnavailable
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.persistence import ObservationStorePort


class AccountSyncService:
    def __init__(self, account: AccountPort, store: ObservationStorePort, clock: ClockPort):
        self.account, self.store, self.clock = account, store, clock
        self._lock = asyncio.Lock()
        self._reports: dict[tuple[str, str], tuple[float, SyncReport]] = {}

    async def sync(self, account_ref: str, symbol: str) -> SyncReport:
        ref = live_account_ref(account_ref)
        if symbol != "BTCUSDT":
            raise ValueError("unsupported sync symbol")
        scope = ref, symbol
        async with self._lock:
            cached = self._reports.get(scope)
            if cached is not None and self.clock.monotonic() < cached[0]:
                return cached[1].model_copy(update={"cached": True})
            cursor = await self.store.cursor(ref, symbol)
            failure, orders, orders_as_of, delay = None, (), None, 15
            try:
                snapshot = await self.account.snapshot(ref)
                orders = await self.account.orders(ref, symbol)
                orders_as_of = self.clock.utcnow()
                batch = await self.account.trades(ref, symbol, cursor)
                if (
                    snapshot.account_ref != ref
                    or snapshot.market_type != "spot"
                    or snapshot.status != AccountSyncStatus.FRESH
                    or snapshot.as_of > self.clock.utcnow()
                ):
                    raise AccountReadUnavailable("invalid_data")
                if (
                    batch.account_ref != ref
                    or batch.symbol != symbol
                    or batch.market_type != "spot"
                ):
                    raise AccountReadUnavailable("invalid_data")
                if len({order.order_id for order in orders}) != len(orders) or any(
                    order.account_ref != ref
                    or order.symbol != symbol
                    or order.market_type != "spot"
                    or order.status.terminal
                    or order.updated_at > orders_as_of
                    for order in orders
                ):
                    raise AccountReadUnavailable("invalid_data")
            except AccountReadUnavailable as error:
                failure, delay = error.reason, max(15, error.retry_after_seconds)
                snapshot = AccountSnapshot(
                    account_ref=ref, as_of=self.clock.utcnow(), status=AccountSyncStatus.UNAVAILABLE
                )
                batch = TradeBatch(account_ref=ref, symbol=symbol, next_cursor=cursor)
                orders, orders_as_of = (), None
            attempted = self.clock.utcnow()
            event = JournalEvent(
                event_id="account-sync:" + str(uuid4()),
                aggregate_id=ref,
                kind="trades_imported",
                payload=batch,
                occurred_at=attempted,
            )
            result = await self.store.ingest(batch, snapshot, event)
            current = result.account
            if current is None:
                raise RuntimeError("observation store lacks atomic account receipt")
            position = None
            if current.account_revision > 0:
                quantity = next(
                    (balance.total for balance in current.balances if balance.asset == "BTC"),
                    Decimal(0),
                )
                position = PositionView(symbol=symbol, quantity=quantity, cost_status="unknown")
            report = SyncReport(
                account=current,
                position=position,
                orders=orders,
                orders_as_of=orders_as_of,
                attempted_at=attempted,
                next_attempt_at=self.clock.utcnow() + timedelta(seconds=delay),
                next_cursor=result.next_cursor,
                imported_count=result.imported_count,
                duplicate_count=result.duplicate_count,
                history_complete=batch.history_complete,
                failure_reason=failure,
            )
            self._reports[scope] = self.clock.monotonic() + delay, report
            return report
