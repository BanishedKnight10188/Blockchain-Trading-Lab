"""Explicit strategy test settings and transition alerts; no model invocation."""

import hashlib
from datetime import timedelta

from agent_platform.domain.common import positive_amount, required_identifier
from agent_platform.domain.decisions import DecisionEvent
from agent_platform.domain.market import FeatureSnapshot
from agent_platform.domain.sessions import AgentSession


class TriggerService:
    def __init__(self, *, return_threshold=None, policy_version: str | None = None):
        if (return_threshold is None) != (policy_version is None):
            raise ValueError("signal threshold requires an explicit policy version")
        self.threshold = positive_amount(return_threshold) if return_threshold is not None else None
        self.policy = (
            required_identifier(policy_version)
            if policy_version is not None
            else "signals-unconfigured"
        )
        self.sample_count = 0
        self._quality: dict[str, bool] = {}
        self._beyond: dict[str, bool] = {}
        self._times = {}

    def evaluate(
        self, features: FeatureSnapshot, session: AgentSession
    ) -> tuple[DecisionEvent, ...]:
        self.sample_count += 1
        if session.status != "running":
            return ()
        key = session.session_id
        if key not in self._times and len(self._times) >= 256:
            raise ValueError("trigger service session capacity reached")
        if features.as_of < self._times.get(key, features.as_of):
            return ()
        self._times[key] = features.as_of
        previous = self._quality.get(key, True)
        self._quality[key] = features.warmup_ready
        kind = None
        if not features.warmup_ready:
            self._beyond[key] = False
            if previous:
                kind = "hard_risk"
        elif self.threshold is not None:
            beyond = features.interval_return.copy_abs() >= self.threshold
            if beyond and not self._beyond.get(key, False):
                kind = "market_change"
            self._beyond[key] = beyond
        if kind is None:
            return ()
        raw = f"{key}:{session.style_revision}:{features.snapshot_id}:{kind}:{self.policy}"
        identity = "feature-trigger:" + hashlib.sha256(raw.encode()).hexdigest()
        return (
            DecisionEvent(
                event_id=identity,
                session_id=key,
                kind=kind,
                occurred_at=features.as_of,
                expires_at=features.as_of + timedelta(seconds=60),
                priority=100 if kind == "hard_risk" else 50,
                evidence_ids=(features.snapshot_id,),
            ),
        )
