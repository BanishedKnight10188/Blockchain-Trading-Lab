"""Browser-safe views recheck freshness without triggering any network request."""

import asyncio
from collections.abc import AsyncIterator

from agent_platform.domain.advice_views import AdviceView
from agent_platform.domain.overview import (
    AccountOverview,
    MarketOverview,
    OrderOverview,
    OverviewFrame,
    OverviewView,
)
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.overview import OverviewPort
from agent_platform.ports.runtime import DecisionRuntimeStatusPort
from agent_platform.ports.sessions import PersistenceUnavailable

from .advice_queries import AdviceQueryService


class QueryService:
    def __init__(
        self,
        source: OverviewPort,
        clock: ClockPort,
        *,
        advice: AdviceQueryService | None = None,
        decision_runtime: DecisionRuntimeStatusPort | None = None,
        sessions=None,
    ):
        self.source, self.clock = source, clock
        self.advice, self.decision_runtime = advice, decision_runtime
        self.sessions = sessions

    async def overview(self, frame: OverviewFrame | None = None) -> OverviewView:
        frame = frame if frame is not None else await self.source.latest()
        if self.sessions is not None:
            selected = await self.sessions.active()
            if selected is not None and not selected.analysis_target.legacy_spot:
                return OverviewView(
                    event_id=frame.event_id,
                    mode=frame.mode,
                    captured_at=frame.captured_at,
                    generated_at=self.clock.utcnow(),
                    market=MarketOverview(source="none", status="not_connected"),
                    account=AccountOverview(source="none", status="not_connected"),
                    advice_reason="futures_history_analysis",
                )
        runtime = await self.decision_runtime.latest() if self.decision_runtime else None
        advice = None
        if self.advice is not None:
            try:
                advice = await self.advice.current(frame)
            except PersistenceUnavailable:
                advice = AdviceView(
                    status="unavailable",
                    reasons=("persistence",),
                    usage_status="unavailable",
                )
        if runtime is not None and runtime.error is not None:
            advice = (
                advice.model_copy(
                    update={
                        "status": "unavailable",
                        "action": None,
                        "explanation": None,
                        "quantity": None,
                        "reasons": (runtime.error,),
                    }
                )
                if advice is not None
                else AdviceView(
                    status="unavailable",
                    reasons=(runtime.error,),
                    usage_status="unavailable",
                )
            )
        now = self.clock.utcnow()
        snapshot = frame.market
        market = MarketOverview(
            source=frame.market_source,
            status="not_connected" if frame.market_source == "none" else "unavailable",
            error=frame.market_error,
        )
        if snapshot is not None and snapshot.as_of <= now:
            status = snapshot.status.value
            if (
                snapshot.latest_quote_at is None
                or (now - snapshot.latest_quote_at).total_seconds() > 5
            ):
                if snapshot.latest_trade is not None or snapshot.book is not None:
                    status = "stale"
            bid, ask = (snapshot.book.bid, snapshot.book.ask) if snapshot.book else (None, None)
            book_status = "unavailable"
            if snapshot.book is not None and snapshot.book_as_of is not None:
                book_status = (
                    "ready" if (now - snapshot.book_as_of).total_seconds() <= 5 else "stale"
                )
            price = snapshot.latest_trade.price if snapshot.latest_trade else None
            features = frame.features
            feature_status = "unavailable"
            if features is not None:
                feature_status = "ready" if features.warmup_ready else "warming"
                if (now - features.as_of).total_seconds() > 60:
                    feature_status = "stale"
            market = MarketOverview(
                source=frame.market_source,
                status=status,
                as_of=snapshot.as_of,
                quote_at=snapshot.latest_quote_at,
                book_at=snapshot.book_as_of,
                book_status=book_status,
                price=price,
                bid=bid,
                ask=ask,
                features=features,
                feature_status=feature_status,
                error=frame.market_error,
            )
        report = frame.sync
        account = AccountOverview(
            source=frame.account_source,
            status="not_connected" if frame.account_source == "none" else "unavailable",
            error=frame.account_error,
        )
        if report is not None and report.attempted_at <= now:
            known = report.account.account_revision > 0
            status = report.account.status
            if status == "fresh" and (now - report.account.as_of).total_seconds() > 60:
                status = "stale"
            orders = tuple(
                OrderOverview(
                    side=item.side.value,
                    status=item.status.value,
                    quantity=item.quantity,
                    filled_quantity=item.filled_quantity,
                    price=item.price,
                    updated_at=item.updated_at,
                )
                for item in report.orders[:20]
            )
            account = AccountOverview(
                source=frame.account_source,
                status=status,
                as_of=report.account.as_of if known else None,
                attempted_at=report.attempted_at,
                next_attempt_at=report.next_attempt_at,
                balances=tuple(
                    item for item in report.account.balances if item.asset in ("BTC", "USDT")
                )
                if known
                else (),
                quantity=report.position.quantity if known and report.position else None,
                orders=orders,
                order_count=len(report.orders) if report.orders_as_of else None,
                orders_as_of=report.orders_as_of,
                orders_status=(
                    "fresh" if (now - report.orders_as_of).total_seconds() <= 60 else "stale"
                )
                if report.orders_as_of is not None
                else "unavailable",
                history_complete=report.history_complete,
                error=frame.account_error or report.failure_reason,
            )
        if advice is not None and advice.status in ("published", "pending"):
            status, reason = None, None
            if advice.expires_at is not None and now >= advice.expires_at:
                status = "expired" if advice.status == "published" else "unavailable"
                reason = "advice_expired" if advice.status == "published" else "unfinished_request"
            elif advice.status == "published" and (
                market.status != "ready"
                or market.feature_status != "ready"
                or account.status != "fresh"
                or (advice.quantity is not None and market.book_status != "ready")
            ):
                status, reason = "unavailable", "current_evidence_unavailable"
            if status is not None:
                advice = advice.model_copy(
                    update={
                        "status": status,
                        "reasons": (reason,),
                        "action": None,
                        "explanation": None,
                        "quantity": None,
                    }
                )
        return OverviewView(
            event_id=frame.event_id,
            mode=frame.mode,
            generated_at=now,
            captured_at=frame.captured_at,
            market=market,
            account=account,
            advice=advice,
            decision_runtime=runtime,
            advice_status=advice.status if advice else "unavailable",
            advice_reason=(advice.reasons[0] if advice.reasons else None)
            if advice
            else "decision_pipeline_not_connected",
        )

    async def watch(self, last_event_id: str | None = None) -> AsyncIterator[OverviewView | None]:
        # Reconnect sends exactly the current frame, including after a process restart.
        view = await self.overview()
        yield view
        while True:
            try:
                async with asyncio.timeout(15):
                    frame = await self.source.wait(view.event_id)
            except TimeoutError:
                view = await self.overview()
                yield view  # A stalled sampler must not freeze ready/fresh labels.
            else:
                view = await self.overview(frame)
                yield view
