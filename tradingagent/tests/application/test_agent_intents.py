import pytest

from tests.fixtures.watch_cases import NOW


def test_protective_stop_required():
    from agent_platform.domain.trade_intents import TradeIntent

    with pytest.raises(ValueError):
        TradeIntent.model_validate({"intent_id": "no-stop", "action": "open_long", "quantity": "1"})


@pytest.mark.asyncio
async def test_paper_open_reduce_and_archive(tmp_path):
    from tests.integration.test_event_agent_paper import prepared

    (
        value,
        runs,
        protection,
        backend,
        execution,
        intents,
        call,
        run,
        market,
        clock,
        guardian,
    ) = await prepared(tmp_path)
    result = await intents.execute_tool(call, run, value)
    assert result["execution"]["receipt"]["status"] == "filled"
    account = await backend.account(value.scope, clock.utcnow())
    assert account.quantity == 1 and account.fees_usdt > 0
    again = await intents.execute_tool(call, run, value)
    assert again == result
    assert len(await backend.recent(value.scope)) == 1
    # Agent pause cannot revoke the independently prepared protective reduction.
    await runs.save_lane(value.model_copy(update={"revision": 2, "enabled": False}), 1)
    clock.advance_to(NOW.replace(second=1))
    market.price = "90"
    await guardian.step(value.scope)
    assert (await backend.account(value.scope, clock.utcnow())).quantity == 0
    assert len(await backend.recent(value.scope)) == 2
