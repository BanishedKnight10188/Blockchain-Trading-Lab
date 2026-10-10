"""One backend-neutral durable submission path; uncertain commands are queried only."""

import asyncio
from datetime import timedelta

from agent_platform.domain.common import utc_datetime
from agent_platform.domain.futures_market import FuturesMarketSnapshot
from agent_platform.domain.trading_execution import (
    TERMINAL_STATUSES,
    ExecutionReceipt,
    TradeCommand,
    TradingAccountSnapshot,
)
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.futures_market import FuturesMarketPort
from agent_platform.ports.trading_execution import (
    ExecutionJournalPort,
    ExecutionRejected,
    FuturesExecutionPort,
    FuturesTradingAccountPort,
)


class TradeExecutionService:
    def __init__(
        self,
        *,
        journal: ExecutionJournalPort,
        execution: FuturesExecutionPort,
        accounts: FuturesTradingAccountPort,
        market: FuturesMarketPort,
        clock: ClockPort,
        market_source: str,
        authorizer=None,
    ):
        if market_source not in ("offline_replay", "binance_futures_public"):
            raise ValueError("execution service requires an explicit supported market source")
        self.journal, self.execution, self.accounts = journal, execution, accounts
        self.market, self.clock = market, clock
        self.market_source = market_source
        self.authorizer = authorizer
        self._lock = asyncio.Lock()

    def _now(self):
        return utc_datetime(self.clock.utcnow())

    async def _observe(self, receipt, *, quote=None):
        current = await self.journal.get(receipt.command.command_id)
        return await self.journal.save(receipt, current.revision, quote=quote)

    async def _unknown(self, command_id, reason):
        current = await self.journal.get(command_id)
        if current.receipt.status in TERMINAL_STATUSES:
            return current
        receipt = ExecutionReceipt.model_validate(
            current.receipt.model_dump()
            | {"status": "unknown", "reason": reason, "observed_at": self._now()}
        )
        return await self.journal.save(receipt, current.revision)

    async def _reconcile(self, record):
        if record.receipt.status in TERMINAL_STATUSES:
            return record
        try:
            receipt = await self.execution.lookup(record.command, self._now())
            if receipt is None:
                return await self._unknown(record.command.command_id, "not_found")
            receipt = ExecutionReceipt.model_validate(receipt)
            if receipt.command != record.command or receipt.observed_at > self._now():
                raise ValueError("backend returned mismatched or future execution")
        except Exception:
            return await self._unknown(record.command.command_id, "execution_unknown")
        return await self._observe(receipt)

    async def reconcile(self, command_id):
        async with self._lock:
            return await self._reconcile(await self.journal.get(command_id))

    def _quote_now(self, command, quote, at):
        if (
            quote.symbol != command.scope.symbol
            or quote.source != self.market_source
            or quote.received_at > at
            or at - min(quote.received_at, quote.mark_at, quote.book_at) > timedelta(seconds=5)
        ):
            raise ExecutionRejected("current quote is unavailable")

    @staticmethod
    def _preflight(command, snapshot, account, at):
        quote = snapshot.quote
        if (
            not command.created_at <= at <= command.expires_at
            or quote.symbol != command.scope.symbol
            or quote.received_at > at
            or at - min(quote.received_at, quote.mark_at, quote.book_at) > timedelta(seconds=5)
            or account.scope != command.scope
            or (
                account.status != "running"
                and not (
                    account.status == "paused"
                    and command.action == "reduce"
                    and getattr(command.decision_evidence, "kind", None) == "guardian"
                )
            )
            or account.revision != command.expected_account_revision
            or account.captured_at > at
            or at - account.captured_at > timedelta(seconds=5)
            or (account.quote is not None and account.quote.source != quote.source)
        ):
            raise ExecutionRejected("current execution evidence no longer authorizes command")

    async def submit(self, command, *, preflight=None):
        command = TradeCommand.model_validate(command)
        async with self._lock:
            record, fresh = await self.journal.reserve(command, self._now())
            if not fresh:
                return await self._reconcile(record)
            attempted = False
            try:
                snapshot = FuturesMarketSnapshot.model_validate(
                    await self.market.snapshot(command.scope.symbol)
                )
                self._quote_now(command, snapshot.quote, self._now())
                account = TradingAccountSnapshot.model_validate(
                    await self.accounts.account(command.scope, self._now())
                )
                at = self._now()
                self._preflight(command, snapshot, account, at)
                if self.authorizer is None:
                    raise ExecutionRejected("execution_authorizer_unavailable")
                await self.authorizer.authorize(command, snapshot, account, at)
                if preflight is not None:
                    preflight(snapshot.quote, account)
                prepared = ExecutionReceipt(command=command, status="pending", observed_at=at)
                await self._observe(prepared, quote=snapshot.quote)
                attempted = True
                receipt = ExecutionReceipt.model_validate(
                    await self.execution.submit(command, snapshot.quote, self._now())
                )
                if receipt.command != command or receipt.observed_at > self._now():
                    raise ValueError("backend returned mismatched or future execution")
            except asyncio.CancelledError:
                await asyncio.shield(self._unknown(command.command_id, "execution_unknown"))
                raise
            except ExecutionRejected:
                receipt = ExecutionReceipt(
                    command=command,
                    status="rejected",
                    reason="preflight_rejected",
                    observed_at=self._now(),
                )
            except Exception:
                if attempted:
                    return await self._unknown(command.command_id, "execution_unknown")
                receipt = ExecutionReceipt(
                    command=command,
                    status="rejected",
                    reason="preflight_rejected",
                    observed_at=self._now(),
                )
            return await self._observe(receipt)
