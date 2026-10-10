import pytest

from tests.application.test_agent_orchestrator import setup


@pytest.mark.asyncio
async def test_no_self_wake_storm(tmp_path):
    from agent_platform.runtime.event_agent import EventAgentRuntime

    value, runs, watches, events, model, agent, tools = await setup(tmp_path)
    await agent.analyze(value, request_id="one")
    runtime = EventAgentRuntime(
        lane_id=value.lane_id, runs=runs, events=events, orchestrator=agent, clock=agent.clock
    )
    for _ in range(5):
        await runtime.step()
    assert len(model.calls) == 2
