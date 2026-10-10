"""A local monitor schedules one isolated decision task; cache sampling owns no model."""

import asyncio
from collections import deque
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from agent_platform.application.decisions import DecisionService
from agent_platform.application.snapshots import SnapshotFactory
from agent_platform.application.triggers import TriggerService
from agent_platform.domain.decisions import DecisionEvent
from agent_platform.domain.runtime_views import DecisionRuntimeView, HardAlertView
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.overview import OverviewPort
from agent_platform.ports.sessions import PersistenceUnavailable, SessionStorePort
from agent_platform.runtime.scheduler import DecisionScheduler


class DecisionRuntime:
    def __init__(
        self,
        sessions: SessionStorePort,
        source: OverviewPort,
        snapshots: SnapshotFactory,
        decisions: DecisionService,
        clock: ClockPort,
    ):
        self.sessions, self.source, self.snapshots, self.decisions, self.clock = (
            sessions,
            source,
            snapshots,
            decisions,
            clock,
        )
        self.triggers = TriggerService()
        self._monitor_task = self._decision_task = None
        self._scheduler = None
        self._session_key = self._account_revision = self._quality = None
        self._session_status = None
        self._alerts = deque(maxlen=16)
        self._error = None
        self._reason = "stopped"
        self._tick_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._monitor_task is not None and not self._monitor_task.done()

    @property
    def inflight(self) -> bool:
        return self._decision_task is not None and not self._decision_task.done()

    @property
    def last_failure(self):
        return self._error

    async def latest(self) -> DecisionRuntimeView:
        now = self.clock.utcnow()
        return DecisionRuntimeView(
            running=self.running,
            inflight=self.inflight,
            session_status=self._session_status,
            reason=self._reason,
            error=self._error,
            alerts=tuple(
                item
                for item in self._alerts
                if timedelta(0) <= now - item.occurred_at <= timedelta(seconds=60)
            ),
        )

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("decision runtime already started")
        self._monitor_task = asyncio.create_task(self._monitor(), name="btc-decision-monitor")
        await asyncio.sleep(0)

    async def stop(self) -> None:
        monitor, self._monitor_task = self._monitor_task, None
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        decision = self._decision_task
        if decision:
            self._cancel_decision()
            await asyncio.gather(decision, return_exceptions=True)
        self._decision_task = None
        self._reason = self._error or "stopped"

    def _cancel_decision(self):
        task = self._decision_task
        # The first cancellation starts settlement. Repeated monitor ticks must not
        # interrupt that audited cleanup while SQLite or the budget port is waiting.
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    def _event(self, session, kind, marker):
        now = self.clock.utcnow()
        return DecisionEvent(
            event_id="runtime:" + sha256(marker.encode()).hexdigest(),
            session_id=session.session_id,
            kind=kind,
            occurred_at=now,
            expires_at=now + timedelta(seconds=60),
            priority=80,
        )

    def _quality_reasons(self, frame):
        now, reasons = self.clock.utcnow(), []
        if frame.market is None or frame.market.status != "ready":
            reasons.append("market_not_ready")
        elif frame.market.latest_quote_at is None or not timedelta(
            0
        ) <= now - frame.market.latest_quote_at <= timedelta(seconds=5):
            reasons.append("market_quote_stale")
        if frame.features is None or not frame.features.warmup_ready:
            reasons.append("features_not_ready")
        elif not timedelta(0) <= now - frame.features.as_of <= timedelta(seconds=60):
            reasons.append("features_stale")
        if (
            frame.sync is None
            or frame.account_error is not None
            or (
                frame.sync.account.status != "fresh"
                or frame.sync.account.account_revision < 1
                or not timedelta(0) <= now - frame.sync.account.as_of <= timedelta(seconds=60)
            )
        ):
            reasons.append("account_unavailable")
        return tuple(reasons)

    async def tick(self) -> None:
        async with self._tick_lock:
            if self._error:
                return
            try:
                await self._tick()
            except PersistenceUnavailable:
                self._error = self._reason = "persistence"
                self._cancel_decision()
            except Exception:
                self._error = self._reason = "invalid_data"
                self._cancel_decision()

    async def _tick(self):
        session = await self.sessions.active()
        self._session_status = session.status if session else None
        if (
            session is None
            or session.status != "running"
            or not session.analysis_target.legacy_spot
        ):
            self._reason = "no_session" if session is None else "session_not_running"
            self._scheduler = self._session_key = self._account_revision = self._quality = None
            if self.inflight:
                self._cancel_decision()
            return
        key = session.session_id, session.revision
        if key != self._session_key:
            if self._session_key is None or self._session_key[0] != session.session_id:
                self._alerts.clear()
            self._session_key, self._account_revision, self._quality = key, None, None
            self._scheduler = DecisionScheduler(session.session_id, self.clock)
            self._scheduler.notify(self._event(session, "manual", f"start:{key}"))
        frame = await self.source.latest()
        quality = self._quality_reasons(frame)
        if quality != self._quality:
            if self._quality and not quality:
                self._scheduler.notify(
                    self._event(
                        session,
                        "manual",
                        f"recovered:{key}:{frame.event_id}",
                    )
                )
            self._quality = quality
            if quality:
                self._alerts.append(HardAlertView(occurred_at=self.clock.utcnow(), reasons=quality))
        if frame.features:
            for event in self.triggers.evaluate(frame.features, session):
                if event.kind != "hard_risk":  # Immediate data alerts above never await the model.
                    self._scheduler.notify(event)
        if frame.sync:
            account = frame.sync.account
            btc = next(
                (balance.total for balance in account.balances if balance.asset == "BTC"),
                Decimal(0),
            )
            self._scheduler.set_holding(btc > 0)
            if (
                self._account_revision is not None
                and self._account_revision != account.account_revision
            ):
                self._scheduler.notify(
                    self._event(
                        session,
                        "account_change",
                        f"account:{key}:{account.account_revision}",
                    )
                )
            self._account_revision = account.account_revision
        self._reason = "deciding" if self.inflight else "waiting"
        if self.inflight:
            return
        if quality:
            self._reason = "current_evidence_unavailable"
            return  # Keep the unconsumed start intent until evidence becomes usable.
        scheduler = self._scheduler
        trigger = scheduler.poll()
        if trigger is None:
            return
        snapshot = await self.snapshots.for_trigger(trigger)
        if snapshot is None:
            scheduler.complete(trigger.trigger_id)
            self._reason = "current_evidence_unavailable"
            return
        request = self.decisions.prepare(snapshot, request_id="request:" + uuid4().hex)
        self._reason = "deciding"
        self._decision_task = asyncio.create_task(
            self._decide(request, scheduler, trigger),
            name="btc-decision-worker",
        )

    async def _decide(self, request, scheduler, trigger):
        try:
            await self.decisions.decide(request)
        except PersistenceUnavailable:
            self._error = self._reason = "persistence"
        except Exception:
            self._error = self._reason = "invalid_data"
        finally:
            scheduler.complete(trigger.trigger_id)
            self._decision_task = None
            if self._error is None:
                self._reason = "waiting"

    async def _monitor(self):
        while True:
            await self.tick()
            await asyncio.sleep(1)
