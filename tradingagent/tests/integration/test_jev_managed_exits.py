"""JEV receives held PnL and chooses an exit; no fixed TP/SL trigger or paid call."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.trading_runtime import TradingLimits
from tests.domain.test_futures_paper import NOW, settings_data
from tests.integration import test_futures_trading_core as core
from tests.integration.test_jev_parameter_trading import parameter_policy

assembled = core.assembled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "direction,mark,exit_choice,expected_pnl,remaining",
    [
        ("LONG", "2100", "REDUCE_Q20", "100", "0.8"),
        ("LONG", "1990", "CLOSE", "-10", "0"),
        ("SHORT", "1900", "CLOSE", "100", "0"),
        ("SHORT", "2010", "REDUCE_Q20", "-10", "0.8"),
    ],
)
async def test_jev_can_reduce_or_close_profit_and_loss_without_fixed_triggers(
    assembled, monkeypatch, direction, mark, exit_choice, expected_pnl, remaining
):
    svc, clock, _, _ = assembled
    run = await svc.configure(
        TradingLimits(**(settings_data() | {"max_leverage": 10})),
        parameter_policy(),
        session_id="s1",
        style_revision=1,
    )
    account = await svc.backend.account(run.scope, clock.utcnow())
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=account.revision,
        style_revision=1,
        trader_revision=1,
    )
    captured = []
    decide = svc.model.decide

    async def record(request):
        captured.append(request)
        return await decide(request)

    monkeypatch.setattr(svc.model, "decide", record)
    svc.model.choices = (f"OPEN_{direction}_M20_L10", exit_choice)
    assert (await svc.step()).status == "filled"
    clock.advance_to(NOW + timedelta(seconds=1))
    svc.maintenance.market.price = Decimal(mark)
    exit_cycle = await svc.step()
    assert exit_cycle.status == "filled" and exit_cycle.plan.candidate_id == exit_choice
    context = json.loads(captured[-1].state_json)
    managed = context["position_management"]
    assert managed["take_profit_mode"] == managed["stop_loss_mode"] == "jev_decision"
    assert managed["fixed_price_triggers"] is None
    assert managed["exchange_protective_orders"] is False
    assert Decimal(managed["entry_price"]) == Decimal("2000")
    assert Decimal(context["account"]["unrealized_pnl_usdt"]) == Decimal(expected_pnl)
    assert captured[-1].question_set_version == "futures-plan-v3"
    assert "take-profit" in captured[-1].questions[0].instructions
    assert "stop-loss" in captured[-1].questions[0].instructions
    after = await svc.backend.account(run.scope, clock.utcnow())
    assert after.quantity == Decimal(remaining)
    assert after.side == (direction.lower() if after.quantity else None)
    assert len(captured) == 2  # Exit is a JEV choice, not an extra threshold-triggered order.
