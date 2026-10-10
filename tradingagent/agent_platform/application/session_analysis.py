"""One independent initial history assessment per selected perpetual session."""

import asyncio
from datetime import timedelta

from agent_platform.domain.futures_chart import ChartHistory, ChartRequest
from agent_platform.domain.session_analysis import (
    InitialAnalysisRecord,
    InitialAnalysisRequest,
    InitialAnalysisResult,
)
from agent_platform.domain.session_market import HistoricalMarketData
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.session_analysis import HistoricalDataUnavailable

HISTORY_RETRY_DELAY = timedelta(seconds=60)


class InitialAnalysisService:
    def __init__(self, *, sessions, store, clock, history=None, model=None, chart=None):
        self.sessions, self.store, self.clock = sessions, store, clock
        self.history, self.model = history, model
        self.chart_provider = chart
        self._lock = asyncio.Lock()

    async def contracts(self):
        if self.history is None:
            raise HistoricalDataUnavailable("public_history_disabled")
        return await self.history.catalog()

    async def validate_target(self, target):
        if target.legacy_spot:
            return
        if not any(
            c.symbol == target.symbol and c.market == target.market for c in await self.contracts()
        ):
            raise ValueError("contract_not_available")

    async def chart_history(self, interval, limit):
        session = await self.sessions.active()
        if session is None or session.analysis_target.legacy_spot:
            raise ValueError("select_futures_session")
        provider = self.chart_provider or self.history
        if provider is None:
            raise HistoricalDataUnavailable("public_history_disabled")
        request = ChartRequest(
            symbol=session.analysis_target.symbol, interval=interval, limit=limit
        )
        async with asyncio.timeout(20):
            history = await provider.chart(request)
        history = ChartHistory.model_validate_json(history.model_dump_json())
        current = await self.sessions.active()
        if (
            current is None
            or current.session_id != session.session_id
            or current.analysis_target != session.analysis_target
            or history.symbol != request.symbol
            or history.interval != request.interval
            or len(history.candles) > limit
            or history.captured_at > self.clock.utcnow()
        ):
            raise ValueError("chart_scope_changed")
        return {"session_id": session.session_id, "history": history.model_dump(mode="json")}

    async def step(self):
        if self._lock.locked():
            return
        async with self._lock:
            session = await self.sessions.active()
            if (
                session is None
                or session.analysis_target.legacy_spot
                or session.status not in ("configured", "running")
            ):
                return
            # Disabled public reads are a visible state, not an irreversible attempt.
            if self.history is None:
                return
            record = await self.store.claim(
                session,
                self.clock.utcnow(),
                resume_prepared=self.model is not None,
                retry_history_after=HISTORY_RETRY_DELAY,
            )
            if record is None:
                return
            history = result = None
            status = "history_unavailable"
            try:
                history = await self.history.history(record.target)
                history = HistoricalMarketData.model_validate_json(history.model_dump_json())
                if history.target != record.target or history.captured_at > self.clock.utcnow():
                    history = None
                    raise ValueError("history scope changed")
                status = "model_unconfigured"
                if self.model is not None:
                    status = "model_failed"
                    at = self.clock.utcnow()
                    current = await self.sessions.active()
                    if not self._current(current, record):
                        status = "discarded"
                    else:
                        request = InitialAnalysisRequest(
                            request_id=record.request_id,
                            session_id=record.session_id,
                            style=record.style,
                            style_revision=record.style_revision,
                            history=history,
                            deadline=at + timedelta(seconds=15),
                        )
                        async with asyncio.timeout(15):
                            result = await self.model.analyze(request)
                        result = InitialAnalysisResult.model_validate_json(result.model_dump_json())
                        if (
                            result.request_id != request.request_id
                            or result.history_hash != history.content_hash
                        ):
                            result = None
                            raise ValueError("answer scope changed")
                        current = await self.sessions.active()
                        status = (
                            "complete"
                            if self._current(current, record)
                            and self.clock.utcnow() < request.deadline
                            else "discarded"
                        )
            except asyncio.CancelledError:
                await self._finish(record, "interrupted", history, None)
                raise
            except (HistoricalDataUnavailable, ValueError, ModelCallFailed, TimeoutError):
                pass
            await self._finish(record, status, history, result)

    @staticmethod
    def _current(session, record):
        return (
            session is not None
            and session.session_id == record.session_id
            and session.style_revision == record.style_revision
            and session.analysis_target == record.target
            and session.status in ("configured", "running")
        )

    async def _finish(self, record, status, history, result):
        await self.store.finish(
            InitialAnalysisRecord.model_validate(
                {
                    **record.model_dump(),
                    "completed_at": self.clock.utcnow(),
                    "status": status,
                    "history": history,
                    "result": result,
                }
            )
        )

    async def public_view(self):
        session = await self.sessions.active()
        record = await self.store.get(session.session_id) if session else None
        reason = (
            "no_session"
            if session is None
            else "legacy_spot"
            if session.analysis_target.legacy_spot
            else "public_history_disabled"
            if self.history is None
            else "pending"
        )
        if record is not None:
            reason = record.status if self._current(session, record) else "style_changed"
        if session is not None and session.status == "paused":
            reason = "paused"
        return {
            "target": session.analysis_target.model_dump(mode="json") if session else None,
            "session_id": session.session_id if session else None,
            "reason": reason,
            "record": record.model_dump(mode="json") if record else None,
            "history_retry_at": (record.completed_at + HISTORY_RETRY_DELAY).isoformat()
            if record
            and record.status == "history_unavailable"
            and record.history is None
            and record.result is None
            and record.completed_at is not None
            and self.history is not None
            and self._current(session, record)
            else None,
            "current_result": record.result.model_dump(mode="json")
            if record and record.status == "complete" and self._current(session, record)
            else None,
            "paid_model_configured": self.model is not None,
            "real_orders_enabled": False,
        }
