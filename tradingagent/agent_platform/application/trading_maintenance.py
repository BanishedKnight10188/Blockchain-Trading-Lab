"""Funding precedes position mutation; maintenance never waits for a decision model."""

from datetime import timedelta

from agent_platform.domain.futures_market import FuturesFundingWindow, FuturesMarketSnapshot
from agent_platform.domain.trading_runtime import FundingCheckpoint
from agent_platform.ports.trading_execution import ExecutionRejected


def milliseconds(at):
    return at.replace(microsecond=at.microsecond // 1000 * 1000)


class TradingMaintenance:
    def __init__(self, *, lifecycle, store, market, clock, market_source):
        self.lifecycle, self.store, self.market, self.clock = lifecycle, store, market, clock
        self.market_source = market_source
        self.latest = {}
        self.failures = {}
        self.selected = None

    async def observe(self, symbol):
        snapshot = FuturesMarketSnapshot.model_validate(await self.market.snapshot(symbol))
        now = self.clock.utcnow()
        q = snapshot.quote
        if (
            q.symbol != symbol
            or q.source != self.market_source
            or q.received_at > now
            or now - min(q.mark_at, q.book_at, q.received_at) > timedelta(seconds=5)
        ):
            raise ExecutionRejected("maintenance_quote_unavailable")
        self.selected = snapshot
        return snapshot

    async def maintain(self, scope, created_at):
        snapshot = await self.observe(scope.symbol)
        q = snapshot.quote
        self.latest[scope.account_ref] = snapshot
        checkpoint = await self.store.checkpoint(scope.account_ref)
        initial = checkpoint is None
        if checkpoint is None:
            checkpoint = FundingCheckpoint(
                account_ref=scope.account_ref,
                cursor=milliseconds(created_at),
                next_due=snapshot.next_funding_at,
            )
            await self.store.save_checkpoint(checkpoint)
        # Retain an already announced due time until its actual event is published.
        through = min(milliseconds(q.mark_at), checkpoint.cursor + timedelta(days=31))
        if through < checkpoint.cursor:
            raise ExecutionRejected("funding_clock_regressed")
        events = ()
        # Between a previously confirmed cursor and its announced future due,
        # there is no regular funding to poll. First use and due/catch-up still
        # require complete exchange evidence; never skip an unpublished event.
        if through > checkpoint.cursor and (
            initial or checkpoint.next_due is None or checkpoint.next_due <= through
        ):
            window = FuturesFundingWindow.model_validate(
                await self.market.settlements(
                    scope.symbol, after=checkpoint.cursor, through=through
                )
            )
            if (
                window.symbol != scope.symbol
                or window.source != self.market_source
                or window.requested_after != checkpoint.cursor
                or window.requested_through != through
                or window.captured_at > self.clock.utcnow()
            ):
                raise ExecutionRejected("funding_window_unavailable")
            events = window.events
            for event in events:
                # A crash after funding but before cursor persistence is recovered by
                # the backend's immutable last_funding_at, never by charging it twice.
                await self.lifecycle.settle(scope, event, self.clock.utcnow())
        due = checkpoint.next_due
        if due is not None and due <= through and not any(e.settled_at == due for e in events):
            raise ExecutionRejected("funding_publication_pending")
        next_due = (
            snapshot.next_funding_at
            if due is None or due <= through
            else min(due, snapshot.next_funding_at)
        )
        await self.store.save_checkpoint(
            FundingCheckpoint(account_ref=scope.account_ref, cursor=through, next_due=next_due)
        )
        if through < milliseconds(q.mark_at):
            raise ExecutionRejected("funding_catchup_pending")
        # If settlement publication is incomplete, do not fabricate liquidation
        # proceeds across an unknown cash flow. Expose the degraded state instead.
        await self.lifecycle.mark(scope, q, self.clock.utcnow())
        self.failures.pop(scope.account_ref, None)
        return snapshot

    async def all(self):
        for scope, created_at in await self.lifecycle.maintenance_accounts():
            try:
                await self.maintain(scope, created_at)
            except Exception as error:
                reason = str(error)
                self.failures[scope.account_ref] = (
                    reason
                    if reason
                    in {
                        "funding_publication_pending",
                        "funding_catchup_pending",
                        "funding_clock_regressed",
                        "maintenance_quote_unavailable",
                        "funding_window_unavailable",
                    }
                    else "maintenance_unavailable"
                )
