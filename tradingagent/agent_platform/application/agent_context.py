"""Compress trigger facts and separately fetched current facts, without inventing data."""

from agent_platform.domain.event_agent import AgentContext, AgentMarketContext, AgentTriggerContext
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.sessions import PersistenceUnavailable


class AgentContextBuilder:
    def __init__(self, *, data, watches, clock, accounts=None):
        self.data, self.watches, self.clock, self.accounts = data, watches, clock, accounts

    async def _build(self, lane, event=None):
        trigger, latest, account, unavailable = None, None, None, []
        if event is not None:
            watch = await self.watches.get(event.watch_id)
            if watch.definition.version != event.definition_revision:
                raise ValueError("trigger definition has changed")
            trigger = AgentTriggerContext(
                event_id=event.event_id, definition=watch.definition, evaluation=event.evaluation
            )
        try:
            frame = await self.data.latest(lane.symbol, event.frame.interval if event else "1m")
            if frame.symbol != lane.symbol or frame.occurred_at > self.clock.utcnow():
                raise ValueError("market scope or time mismatch")
            latest = AgentMarketContext(
                captured_at=frame.occurred_at,
                candle=frame.candles[-1].model_dump(mode="json"),
                features=frame.features,
                quality=frame.effective_quality,
                input_hash=frame.content_hash,
            )
        except (ValueError, OSError, TimeoutError, FuturesMarketUnavailable):
            unavailable.append("market")
        if self.accounts is not None:
            try:
                account = await self.accounts.account(lane.scope, self.clock.utcnow())
            except (ValueError, LookupError, OSError, TimeoutError, PersistenceUnavailable):
                unavailable.append("account")
        else:
            unavailable.append("account")
        return AgentContext(
            lane_id=lane.lane_id,
            scope=lane.scope,
            captured_at=self.clock.utcnow(),
            policy=lane.policy,
            limits=lane.limits,
            lane_revision=lane.revision,
            policy_revision=lane.policy_revision,
            trigger=trigger,
            latest_market=latest,
            account=account,
            unavailable=tuple(unavailable),
        )

    async def for_event(self, event, lane):
        if (event.lane_id, event.session_id) != (lane.lane_id, lane.session_id):
            raise ValueError("event belongs to another lane")
        return await self._build(lane, event)

    async def for_analysis(self, lane):
        return await self._build(lane)
