import pytest

from agent_platform.domain.agent_tools import ToolCall
from tests.application.test_agent_orchestrator import setup
from tests.fixtures.watch_cases import NOW


@pytest.mark.asyncio
async def test_mode_and_scope_permissions(tmp_path):
    value, runs, watches, events, model, agent, tools = await setup(tmp_path)
    run = await runs.claim_analysis(value, "permissions", NOW)
    denied = await tools.execute(
        ToolCall(tool_call_id="x", name="submit_trade_intent", arguments={}), run, value
    )
    assert denied.status == "rejected"
    crossed = await tools.execute(
        ToolCall(tool_call_id="y", name="create_watch", arguments={"lane_id": "other"}), run, value
    )
    assert crossed.status == "rejected"
    assert await watches.list_active(value.lane_id) == ()
