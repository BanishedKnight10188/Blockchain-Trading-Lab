import pytest

from tests.integration.test_event_agent_paper import prepared


@pytest.mark.asyncio
async def test_missing_quote_is_degraded_not_fake_fill(tmp_path):
    (
        value,
        runs,
        store,
        backend,
        execution,
        intents,
        call,
        run,
        market,
        clock,
        guardian,
    ) = await prepared(tmp_path)
    await intents.execute_tool(call, run, value)

    async def unavailable(*args):
        raise OSError("offline")

    market.snapshot = unavailable
    status = await guardian.step(value.scope)
    assert status.status == "degraded"
    assert (await backend.account(value.scope, clock.utcnow())).quantity == 1
    assert len(await backend.recent(value.scope)) == 1
