"""Capture bounded current facts from local caches and authoritative persisted state."""

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from agent_platform.domain.account import PositionView
from agent_platform.domain.common import live_account_ref
from agent_platform.domain.decision_requests import DecisionRequest
from agent_platform.domain.decisions import DecisionSnapshot, DecisionTrigger
from agent_platform.domain.overview import OverviewFrame
from agent_platform.domain.risk import DisciplineLimits
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.overview import OverviewPort
from agent_platform.ports.persistence import ObservationStorePort
from agent_platform.ports.sessions import SessionStorePort


class SnapshotFactory:
    def __init__(
        self,
        sessions: SessionStorePort,
        observations: ObservationStorePort,
        source: OverviewPort,
        clock: ClockPort,
        *,
        account_ref: str | None = None,
    ):
        self.sessions, self.observations, self.source, self.clock = (
            sessions,
            observations,
            source,
            clock,
        )
        self.account_ref = live_account_ref(account_ref) if account_ref is not None else None

    async def account(self):
        return (
            await self.observations.account_snapshot(self.account_ref) if self.account_ref else None
        )

    async def for_trigger(
        self,
        trigger: DecisionTrigger,
        *,
        limits: DisciplineLimits | None = None,
        retained_evidence: tuple[str, ...] = (),
        frame: OverviewFrame | None = None,
    ) -> DecisionSnapshot | None:
        session = await self.sessions.active()
        if (
            session is None
            or session.status != "running"
            or session.session_id != trigger.session_id
            or not session.analysis_target.legacy_spot
        ):
            return None
        frame = frame if frame is not None else await self.source.latest()
        account = await self.account()
        now = self.clock.utcnow()
        market, features, sync = frame.market, frame.features, frame.sync
        if (
            frame.captured_at > now
            or not trigger.requested_at <= now < trigger.expires_at
            or market is None
            or features is None
            or sync is None
            or frame.account_error is not None
            or account is None
            or account.status != "fresh"
            or account.account_revision < 1
            or not timedelta(0) <= now - account.as_of <= timedelta(seconds=60)
            or market.status != "ready"
            or market.latest_quote_at is None
            or not timedelta(0) <= now - market.latest_quote_at <= timedelta(seconds=5)
            or not features.warmup_ready
            or not timedelta(0) <= now - features.as_of <= timedelta(seconds=60)
            or account.market_type != "spot"
            or sync.account != account
        ):
            return None
        # Bound before exact summation/serialization; do not infer a known cost basis.
        if any(
            len(value.as_tuple().digits) > 128 or not -128 <= value.as_tuple().exponent <= 128
            for balance in account.balances
            for value in (balance.free, balance.locked)
        ):
            return None
        btc = next(
            (balance.total for balance in account.balances if balance.asset == "BTC"), Decimal(0)
        )
        account_id = "account:" + sha256(account.model_dump_json().encode()).hexdigest()
        market_id = "market:" + sha256(market.model_dump_json().encode()).hexdigest()
        evidence = tuple(
            dict.fromkeys((*retained_evidence, features.snapshot_id, market_id, account_id))
        )
        if len(evidence) > 64 or any(len(item) > 128 for item in evidence):
            return None
        return DecisionSnapshot(
            snapshot_id="snapshot:" + uuid4().hex,
            session_id=session.session_id,
            session_revision=session.revision,
            style_revision=session.style_revision,
            style=session.style,
            captured_at=now,
            market=market,
            features=features,
            account=account,
            position=PositionView(symbol="BTCUSDT", quantity=btc, cost_status="unknown"),
            trigger=trigger,
            limits=limits or DisciplineLimits(),
            evidence_ids=evidence,
        )

    async def capture(self, request: DecisionRequest) -> DecisionSnapshot | None:
        return await self.for_trigger(
            request.snapshot.trigger,
            limits=request.snapshot.limits,
            retained_evidence=request.snapshot.evidence_ids,
        )
