"""Bounded monotonic scheduling; risk alerts never acquire the decision slot."""

import asyncio
from collections import OrderedDict, deque
from datetime import timedelta
from uuid import uuid4

from agent_platform.domain.common import required_identifier
from agent_platform.domain.decisions import DecisionEvent, DecisionTrigger
from agent_platform.ports.clock import ClockPort


class DecisionScheduler:
    def __init__(self, session_id: str, clock: ClockPort):
        self.session_id, self.clock = required_identifier(session_id), clock
        self._holding = False
        self._next_periodic = clock.monotonic() + 300
        self._last_emitted = {}
        self._pending: OrderedDict[str, tuple[DecisionEvent, float]] = OrderedDict()
        self._seen: OrderedDict[str, DecisionEvent] = OrderedDict()
        self._alerts = deque(maxlen=16)
        self._wake, self._alert_wake = asyncio.Event(), asyncio.Event()
        self._inflight: DecisionTrigger | None = None
        self._inflight_facts: tuple[DecisionEvent, ...] = ()

    @property
    def inflight(self) -> DecisionTrigger | None:
        return self._inflight

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def set_holding(self, holding: bool) -> None:
        if type(holding) is not bool:
            raise ValueError("holding state must be explicit bool")
        if holding != self._holding:
            self._holding = holding
            self._next_periodic = self.clock.monotonic() + (60 if holding else 300)
            self._wake.set()

    def notify(self, event: DecisionEvent) -> None:
        if event.session_id != self.session_id or event.occurred_at > self.clock.utcnow():
            raise ValueError("event scope/time is incompatible with scheduler")
        if (
            len(event.event_id) > 256
            or len(event.evidence_ids) > 32
            or any(len(item) > 256 for item in event.evidence_ids)
        ):
            raise ValueError("decision event exceeds scheduler size limits")
        previous = self._seen.get(event.event_id)
        if previous is None and event.event_id in self._pending:
            previous = self._pending[event.event_id][0]
        if previous is None:
            previous = next(
                (item for item, _ in self._alerts if item.event_id == event.event_id), None
            )
        if previous is None:
            previous = next(
                (item for item in self._inflight_facts if item.event_id == event.event_id), None
            )
        if previous is not None:
            if previous != event:
                raise ValueError("decision event identity already refers to another fact")
            return
        remaining = (event.expires_at - self.clock.utcnow()).total_seconds()
        if remaining <= 0:
            return
        self._seen[event.event_id] = event
        while len(self._seen) > 256:
            self._seen.popitem(last=False)
        deadline = self.clock.monotonic() + remaining
        if event.kind == "hard_risk":
            self._alerts.append((event, deadline))
            self._alert_wake.set()
        else:
            self._pending[event.event_id] = event, deadline
            while len(self._pending) > 64:
                self._pending.popitem(last=False)
            self._wake.set()

    def _prune(self, now: float) -> None:
        for identity, (_, deadline) in tuple(self._pending.items()):
            # datetime UTC facts cannot represent sub-microsecond validity.
            if deadline - now <= 0.000001:
                del self._pending[identity]

    def poll(self) -> DecisionTrigger | None:
        now = self.clock.monotonic()
        self._prune(now)
        if self._inflight is not None:
            return None
        wall = self.clock.utcnow()
        facts = ()
        ready = [
            (event, deadline)
            for event, deadline in self._pending.values()
            if now - self._last_emitted.get(event.kind, float("-inf")) >= 30
        ]
        if ready:
            chosen = max(ready, key=lambda item: (item[0].priority, item[0].occurred_at))[0].kind
            members = [(event, deadline) for event, deadline in ready if event.kind == chosen]
            ids = tuple(event.event_id for event, _ in members)
            ttl = min(60, min(deadline - now for _, deadline in members))
            facts = tuple(event for event, _ in members)
        elif now >= self._next_periodic:
            chosen, ids, ttl = "periodic", (), 60
        else:
            return None
        trigger = DecisionTrigger(
            trigger_id="trigger:" + uuid4().hex,
            session_id=self.session_id,
            kind=chosen,
            requested_at=wall,
            expires_at=wall + timedelta(seconds=ttl),
            event_ids=ids,
        )
        for identity in ids:
            del self._pending[identity]
        self._last_emitted[chosen] = now
        self._next_periodic = now + (60 if self._holding else 300)
        self._inflight = trigger
        self._inflight_facts = facts
        return trigger

    def complete(self, trigger_id: str) -> None:
        if self._inflight is None or trigger_id != self._inflight.trigger_id:
            raise ValueError("completion does not own the active decision slot")
        self._inflight = None
        self._inflight_facts = ()
        self._wake.set()

    async def next_trigger(self) -> DecisionTrigger:
        while True:
            self._wake.clear()
            ready = self.poll()
            if ready is not None:
                return ready
            timeout = None
            if self._inflight is None:
                now = self.clock.monotonic()
                deadlines = [self._next_periodic]
                for event, expiry in self._pending.values():
                    deadlines.extend((expiry, self._last_emitted.get(event.kind, now - 30) + 30))
                timeout = max(0.001, min(deadlines) - now)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except TimeoutError:
                pass

    async def next_alert(self) -> DecisionEvent:
        while True:
            self._alert_wake.clear()
            while self._alerts:
                event, deadline = self._alerts.popleft()
                if deadline > self.clock.monotonic():
                    return event
            await self._alert_wake.wait()
