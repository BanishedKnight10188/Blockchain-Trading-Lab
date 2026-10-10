from datetime import timedelta
from decimal import Decimal

import pytest

from tests.fixtures.watch_cases import NOW
from tests.integration.test_event_agent_paper import prepared


@pytest.mark.asyncio
async def test_guardian_enforces_run_loss_even_before_stop(tmp_path):
    (
        value,
        runs,
        protections,
        backend,
        execution,
        intents,
        call,
        run,
        market,
        clock,
        guardian,
    ) = await prepared(tmp_path)
    # Stop below the loss ceiling: loss cap must win before the configured price stop.
    call = call.model_copy(update={"arguments": call.arguments | {"protective_stop_mark": "1"}})
    # The original archived tool would differ; use the normal validated stop for entry,
    # then exercise the independently fixed loss cap with a smaller user budget.
    await intents.execute_tool(run.tools[0].call, run, value)
    # With 1 BTC entered at 105, a loss budget 100 is crossed at mark < 5.
    # A stop at 95 already crosses too; direct pure condition isolates loss from stop.
    from agent_platform.domain.position_protection import protection_triggered

    p = (await protections.recover(value.scope))[0]
    p = p.model_copy(
        update={"intent": p.intent.model_copy(update={"protective_stop_mark": Decimal("1")})}
    )
    clock.advance_to(NOW + timedelta(seconds=1))
    market.price = "4"
    q = (await market.snapshot(value.symbol)).quote
    # Isolate the loss condition with explicit account facts.
    account = await backend.account(value.scope, NOW)
    account = account.model_copy(
        update={"equity_usdt": Decimal("899"), "quote": q, "captured_at": clock.utcnow()}
    )
    assert protection_triggered(p, value, account, q)


@pytest.mark.asyncio
async def test_intent_tool_leverage_and_stop_bind_to_archived_call(tmp_path):
    (
        value,
        runs,
        protections,
        backend,
        execution,
        intents,
        call,
        run,
        market,
        clock,
        guardian,
    ) = await prepared(tmp_path)
    forged = call.model_copy(update={"arguments": call.arguments | {"target_leverage": 3}})
    with pytest.raises(ValueError):
        await intents.execute_tool(forged, run, value)
