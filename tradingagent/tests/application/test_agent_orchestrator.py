import asyncio
from datetime import timedelta

import pytest

from agent_platform.adapters.sqlite.agent_events import SqliteAgentEventStore
from agent_platform.adapters.sqlite.event_agent import SqliteAgentRunStore
from agent_platform.adapters.sqlite.watches import SqliteWatchStore
from agent_platform.domain.agent_tools import ToolCall
from tests.fixtures.event_agent_cases import lane, response
from tests.fixtures.watch_cases import NOW, frame


class Clock:
    def utcnow(self):
        return NOW

    def monotonic(self):
        return 0


class Data:
    async def latest(self, symbol, interval):
        return frame()


class Model:
    paid = False

    def __init__(self):
        self.calls = []

    async def turn(self, request):
        self.calls.append(request)
        if len(self.calls) == 1:
            return response(
                request.request_id,
                calls=(
                    ToolCall(
                        tool_call_id="create-1",
                        name="create_watch",
                        arguments={
                            "timeframe": "1m",
                            "hypothesis": "reclaim",
                            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                            "trigger": {
                                "logic": "ALL",
                                "conditions": [
                                    {"metric": "candle.close", "op": "GT", "value": "104"}
                                ],
                            },
                        },
                    ),
                ),
            )
        return response(request.request_id)


async def setup(tmp_path, model=None):
    from agent_platform.application.agent_context import AgentContextBuilder
    from agent_platform.application.agent_orchestrator import AgentOrchestrator
    from agent_platform.application.agent_tools import ToolRegistry
    from agent_platform.domain.costs import RouteDecision

    path = tmp_path / "agent.sqlite"
    runs, watches, events = (
        SqliteAgentRunStore(path),
        SqliteWatchStore(path),
        SqliteAgentEventStore(path),
    )
    await runs.initialize()
    value = lane()
    await runs.save_lane(value, 0)
    clock = Clock()
    context = AgentContextBuilder(data=Data(), watches=watches, clock=clock)
    tools = ToolRegistry(runs=runs, watches=watches, context=context, clock=clock)
    route = RouteDecision(
        route_id="event-agent",
        kind="standard",
        purpose="advisory",
        model_version="test/model",
        reason="offline_test",
        paid=False,
    )
    model = model or Model()
    orchestrator = AgentOrchestrator(
        runs=runs,
        events=events,
        context=context,
        tools=tools,
        model=model,
        route=route,
        clock=clock,
    )
    return value, runs, watches, events, model, orchestrator, tools


@pytest.mark.asyncio
async def test_analysis_creates_watch_then_review(tmp_path):

    value, runs, watches, events, model, agent, tools = await setup(tmp_path)
    analyzed = await agent.analyze(value, request_id="analysis-1")
    assert analyzed.status == "WAIT"
    assert len(await watches.list_active(value.lane_id)) == 1
    assert await agent.analyze(value, request_id="analysis-1") == analyzed
    assert len(model.calls) == 2
    # A candle closed after watch creation is needed; do not reuse a prior signal.
    from agent_platform.domain.watch_rules import evaluate_watch
    from tests.fixtures.watch_cases import candles

    watch = (await watches.list_active(value.lane_id))[0]
    later = NOW + timedelta(minutes=1)
    snapshot = frame(candles(end=later), received_at=later)
    await watches.commit_evaluation(
        watch.definition.watch_id, watch.revision, snapshot, evaluate_watch(watch, snapshot, later)
    )
    agent.clock.utcnow = lambda: later
    agent.context.data.latest = _latest(snapshot)
    lease = await events.claim(value.lane_id, later, 120)
    reviewed = await agent.review(lease, value)
    assert reviewed.context.trigger.event_id == lease.event.event_id
    assert reviewed.context.latest_market.captured_at >= lease.event.occurred_at
    assert (await events.delivery(lease.event.event_id)).status == "COMPLETED"


def _latest(snapshot):
    async def latest(*args):
        return snapshot

    return latest


@pytest.mark.asyncio
async def test_pause_or_revision_change_discards_inflight(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def turn(self, request):
            entered.set()
            await release.wait()
            return await super().turn(request)

    value, runs, watches, events, model, agent, tools = await setup(tmp_path, Slow())
    task = asyncio.create_task(agent.analyze(value, request_id="pause"))
    await entered.wait()
    paused = value.model_copy(update={"enabled": False, "revision": 2})
    await runs.save_lane(paused, 1)
    release.set()
    result = await task
    assert result.status == "INTERRUPTED"
    assert result.turns[0].response is not None
    assert await watches.list_active(value.lane_id) == ()


@pytest.mark.asyncio
async def test_loop_bounds(tmp_path):
    class Loop(Model):
        async def turn(self, request):
            self.calls.append(request)
            return response(
                request.request_id,
                calls=(
                    ToolCall(
                        tool_call_id=f"read-{len(self.calls)}",
                        name="get_market_snapshot",
                        arguments={},
                    ),
                ),
            )

    value, runs, watches, events, model, agent, tools = await setup(tmp_path, Loop())
    result = await agent.analyze(value, request_id="loop")
    assert len(model.calls) == 4
    assert result.status == "FAILED" and len(result.tools) == 4
