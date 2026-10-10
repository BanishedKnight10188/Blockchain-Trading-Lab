"""Independent assembly and controls; no implicit JEV grants, accounts or switches."""

from dataclasses import dataclass
from pathlib import Path

from agent_platform.adapters.fake.event_agent import OfflineEventAgentModel, OfflineWatchData
from agent_platform.adapters.fake.futures_trading import OfflineFuturesMarket
from agent_platform.adapters.sqlite.agent_events import SqliteAgentEventStore
from agent_platform.adapters.sqlite.event_agent import SqliteAgentRunStore
from agent_platform.adapters.sqlite.event_paper import SqliteEventPaperBackend
from agent_platform.adapters.sqlite.position_protection import SqliteProtectionStore
from agent_platform.adapters.sqlite.trading_execution import SqliteExecutionJournal
from agent_platform.adapters.sqlite.watches import SqliteWatchStore
from agent_platform.application.agent_context import AgentContextBuilder
from agent_platform.application.agent_intents import AgentIntentService
from agent_platform.application.agent_models import BudgetedAgentModel
from agent_platform.application.agent_orchestrator import AgentOrchestrator
from agent_platform.application.agent_tools import ToolRegistry
from agent_platform.application.position_guardian import PositionGuardian
from agent_platform.application.trade_authorization import EventTradeAuthorizer
from agent_platform.application.trading_execution import TradeExecutionService
from agent_platform.application.watches import WatchService
from agent_platform.domain.costs import RouteDecision
from agent_platform.domain.event_agent import EventAgentLane
from agent_platform.domain.watches import WatchDefinition
from agent_platform.ports.sessions import RevisionConflict
from agent_platform.runtime.clock import SystemClock
from agent_platform.runtime.event_agent import EventAgentRuntime
from agent_platform.runtime.position_guardian import PositionGuardianRuntime
from agent_platform.runtime.watches import WatchRuntime


@dataclass
class EventAgentServices:
    lane_id: str
    runs: object
    watches: object
    events: object
    agent: object
    intents: object
    backend: object
    protections: object
    guardian: object
    agent_runtime: object
    watch_runtime: object
    guardian_runtime: object
    clock: object
    model: object
    market_source: str
    running: bool = False

    async def start(self):
        if self.running:
            return
        await self.agent_runtime.start()
        await self.watch_runtime.start()
        await self.guardian_runtime.start()
        self.running = True

    async def stop(self):
        await self.agent_runtime.stop()
        await self.watch_runtime.stop()
        await self.guardian_runtime.stop()
        self.running = False

    async def configure(self, selection, expected_revision):
        lane = await self.runs.lane(self.lane_id)
        if lane.revision != expected_revision:
            raise RevisionConflict("lane_changed")
        candidate = EventAgentLane.model_validate(
            lane.model_dump()
            | selection
            | {
                "revision": lane.revision + 1,
                "enabled": False,
                "policy_revision": lane.policy_revision + 1,
            }
        )
        lane = await self.runs.save_lane(candidate, expected_revision)
        await self.backend.configure(lane, self.clock.utcnow())
        await self.backend.set_enabled(lane, self.clock.utcnow())
        return lane

    async def set_enabled(self, enabled, expected_revision):
        lane = await self.runs.lane(self.lane_id)
        lane = await self.runs.save_lane(
            lane.model_copy(update={"revision": lane.revision + 1, "enabled": enabled}),
            expected_revision,
        )
        if lane.risk_configured:
            await self.backend.set_enabled(lane, self.clock.utcnow())
        return lane

    async def analyze(self, expected_revision, request_id):
        lane = await self.runs.lane(self.lane_id)
        if lane.revision != expected_revision:
            raise RevisionConflict("lane_changed")
        return await self.agent.analyze(lane, request_id)

    async def create_watch(self, definition, expected_revision):
        lane = await self.runs.lane(self.lane_id)
        if lane.revision != expected_revision:
            raise RevisionConflict("lane_changed")
        value = WatchDefinition.model_validate_json(__import__("json").dumps(definition))
        if (value.lane_id, value.session_id, value.symbol) != (
            lane.lane_id,
            lane.session_id,
            lane.symbol,
        ):
            raise ValueError("watch_scope")
        return await self.watches.create(value)

    async def status(self):
        lane = await self.runs.lane(self.lane_id)
        runs = await self.runs.recent(self.lane_id)
        account = None
        if lane.risk_configured:
            try:
                account = await self.backend.account(lane.scope, self.clock.utcnow())
            except (ValueError, LookupError):
                pass
        active = await self.protections.recover(lane.scope)
        protection = self.guardian_runtime.status
        usages = [
            t.response.usage.model_dump(mode="json") for r in runs for t in r.turns if t.response
        ]
        return {
            "available": True,
            "execution_environment": "paper",
            "lane": lane.model_dump(mode="json"),
            "model_source": "paid_tool_model" if self.model.paid else "offline_fake_wait",
            "market_source": self.market_source,
            "paid_calls_enabled": bool(self.model.paid and getattr(self.model, "grant", None)),
            "protection_status": protection.status
            if protection
            else "ready"
            if active
            else "not_configured",
            "guardian_running": self.guardian_runtime._task is not None,
            "account": account.model_dump(mode="json") if account else None,
            "watches": [
                w.model_dump(mode="json") for w in await self.watches.list_all(self.lane_id)
            ],
            "events": [e.model_dump(mode="json") for e in await self.events.recent(self.lane_id)],
            "runs": [r.model_dump(mode="json") for r in runs],
            "costs": usages,
            "protections": [p.model_dump(mode="json") for p in active],
            "executions": list(await self.backend.recent(lane.scope))
            if lane.risk_configured
            else [],
        }


async def build_event_agent_services(
    database_path: Path,
    *,
    clock=None,
    lane=None,
    data=None,
    market=None,
    model=None,
    route=None,
    market_source="offline_replay",
):
    clock = clock or SystemClock()
    lane = lane or EventAgentLane(
        lane_id="event-lane-1",
        session_id="event-agent-1",
        scope={
            "environment": "paper",
            "account_ref": "paper:futures:event-agent-1",
            "session_id": "event-agent-1",
            "symbol": "BTCUSDT",
        },
    )
    data = data or OfflineWatchData(clock)
    market = market or OfflineFuturesMarket(clock)
    model = model or OfflineEventAgentModel(clock)
    if model.paid and not isinstance(model, BudgetedAgentModel):
        raise ValueError("paid event model requires explicit BudgetedAgentModel and new grant")
    if model.paid and route is None:
        raise ValueError("paid model requires its explicit verified route")
    route = route or RouteDecision(
        route_id="event-agent-offline",
        kind="standard",
        purpose="advisory",
        reason="offline",
        model_version="offline-event-v1",
        paid=False,
    )
    runs = SqliteAgentRunStore(database_path)
    await runs.initialize()
    try:
        persisted = await runs.lane(lane.lane_id)
    except ValueError:
        persisted = await runs.save_lane(lane.model_copy(update={"enabled": False}), 0)
    if persisted.scope != lane.scope:
        raise ValueError("saved lane ownership differs")
    if persisted.enabled:
        persisted = await runs.save_lane(
            persisted.model_copy(update={"enabled": False, "revision": persisted.revision + 1}),
            persisted.revision,
        )
    watches, events = SqliteWatchStore(database_path), SqliteAgentEventStore(database_path)
    protections = SqliteProtectionStore(database_path)
    backend = SqliteEventPaperBackend(database_path, market_source=market_source)
    if persisted.risk_configured:
        await backend.configure(persisted, clock.utcnow())
        await backend.set_enabled(persisted, clock.utcnow())
    journal = SqliteExecutionJournal(database_path)
    await journal.initialize()
    authorizer = EventTradeAuthorizer(
        runs=runs, watches=watches, events=events, data=data, protections=protections
    )
    execution = TradeExecutionService(
        journal=journal,
        execution=backend,
        accounts=backend,
        market=market,
        clock=clock,
        market_source=market_source,
        authorizer=authorizer,
    )
    context = AgentContextBuilder(data=data, watches=watches, clock=clock, accounts=backend)
    intents = AgentIntentService(
        runs=runs,
        protections=protections,
        execution=execution,
        accounts=backend,
        market=market,
        clock=clock,
        authorizer=authorizer,
    )
    tools = ToolRegistry(runs=runs, watches=watches, context=context, clock=clock, intents=intents)
    agent = AgentOrchestrator(
        runs=runs,
        events=events,
        context=context,
        tools=tools,
        model=model,
        route=route,
        clock=clock,
    )
    guardian = PositionGuardian(
        protections=protections,
        runs=runs,
        execution=execution,
        accounts=backend,
        market=market,
        clock=clock,
    )
    return EventAgentServices(
        lane_id=lane.lane_id,
        runs=runs,
        watches=watches,
        events=events,
        agent=agent,
        intents=intents,
        backend=backend,
        protections=protections,
        guardian=guardian,
        clock=clock,
        model=model,
        market_source=market_source,
        agent_runtime=EventAgentRuntime(
            lane_id=lane.lane_id, runs=runs, events=events, orchestrator=agent, clock=clock
        ),
        watch_runtime=WatchRuntime(WatchService(watches, lane_id=lane.lane_id), data, clock),
        guardian_runtime=PositionGuardianRuntime(guardian, lane.scope),
    )
