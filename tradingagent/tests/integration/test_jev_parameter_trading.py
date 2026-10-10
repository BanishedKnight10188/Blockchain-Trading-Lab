"""Parameter decisions with real virtual accounting, no paid or network calls."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_platform.domain.futures_paper import FuturesPaperOrder, FuturesPaperSettings
from agent_platform.domain.futures_paper_engine import execute
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.ports.persistence import EventIdentityConflict
from tests.domain.test_futures_paper import NOW, quote, settings_data, state
from tests.integration import test_futures_trading_core as core

assembled = core.assembled


def parameter_policy():
    return TradingPolicy(
        order_notional_usdt="100",
        max_price_drift_bps="20",
        min_confidence="0.8",
        strategy_instructions="Choose from the complete precomputed plans.",
        decision_mode="parameterized",
        entry_margin_percents=(20,),
        position_change_percents=(20,),
        leverage_choices=(5, 10),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
async def test_parameter_cycle_opens_adds_reduces_and_closes_exact_quantities(assembled, direction):
    svc, clock, _, _ = assembled
    run = await svc.configure(
        TradingLimits(**(settings_data() | {"max_leverage": 10})),
        parameter_policy(),
        session_id="s1",
        style_revision=1,
    )
    a = await svc.backend.account(run.scope, clock.utcnow())
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=a.revision,
        style_revision=1,
        trader_revision=1,
    )
    svc.model.choices = (
        f"OPEN_{direction}_M20_L10",
        f"ADD_{direction}_Q20_L10",
        "REDUCE_Q20",
        "CLOSE",
    )
    quantities = ("1", "1.2", "0.96", "0")
    for index, expected in enumerate(quantities):
        clock.advance_to(NOW + timedelta(seconds=index))
        cycle = await svc.step()
        assert cycle.status == "filled", cycle
        assert cycle.plan.candidate_id == svc.model.choices[index]
        a = await svc.backend.account(run.scope, clock.utcnow())
        assert a.quantity == Decimal(expected)
        assert a.leverage == 10
    assert a.free_usdt == Decimal("997.6")
    assert a.margin_usdt == 0 and a.fees_usdt == Decimal("2.4")
    await svc.recover()
    again = await svc.backend.account(run.scope, clock.utcnow())
    assert again.status == "paused" and again.free_usdt == a.free_usdt
    cycles = await svc.store.recent(run.scope.account_ref)
    assert len(cycles) == 4 and cycles[-1].plan is not None


def test_paper_leverage_change_preserves_cash_and_funding_collateral():
    s = state()
    settings = FuturesPaperSettings(**(settings_data() | {"max_leverage": 10}))
    s = type(s).model_validate(s.model_dump() | {"settings": settings})
    first = execute(
        s, FuturesPaperOrder(action="open_long", quantity="1", target_leverage=10), quote(), NOW
    ).state
    assert first.settings.leverage == 10
    assert first.free_usdt == 799 and first.margin_usdt == 200
    # A recorded funding debit must not disappear when leverage rebalances collateral.
    first = type(first).model_validate(
        first.model_dump() | {"margin_usdt": Decimal(198), "funding_usdt": Decimal(-2)}
    )
    second = execute(
        first,
        FuturesPaperOrder(action="open_long", quantity="0.2", target_leverage=5),
        quote(),
        NOW,
    ).state
    assert second.settings.leverage == 5
    assert second.margin_usdt == 478 and second.free_usdt == Decimal("518.8")
    assert second.free_usdt + second.margin_usdt == Decimal("996.8")
    assert second.funding_usdt == -2


def test_paper_never_raises_legacy_leverage_cap():
    s = state()
    with pytest.raises(ValueError, match="leverage"):
        execute(
            s,
            FuturesPaperOrder(action="open_long", quantity="0.1", target_leverage=10),
            quote(),
            NOW,
        )


def test_lowering_leverage_cannot_create_collateral_when_free_funds_are_insufficient():
    s = state()
    first = execute(s, FuturesPaperOrder(action="open_long", quantity="1"), quote(), NOW).state
    with pytest.raises(ValueError, match="insufficient"):
        execute(
            first,
            FuturesPaperOrder(action="open_long", quantity="0.2", target_leverage=1),
            quote(),
            NOW,
        )
    assert first.settings.leverage == 5 and first.margin_usdt == 400


@pytest.mark.asyncio
async def test_wait_does_not_modify_wallet_or_leverage(assembled):
    svc, clock, _, _ = assembled
    run = await svc.configure(
        TradingLimits(**(settings_data() | {"max_leverage": 10})),
        parameter_policy(),
        session_id="s1",
        style_revision=1,
    )
    a = await svc.backend.account(run.scope, clock.utcnow())
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=a.revision,
        style_revision=1,
        trader_revision=1,
    )
    before = await svc.backend.account(run.scope, clock.utcnow())
    cycle = await svc.step()
    after = await svc.backend.account(run.scope, clock.utcnow())
    assert cycle.status == "wait" and cycle.plan.candidate_id == "WAIT"
    assert after.revision == before.revision and after.leverage == before.leverage
    assert after.free_usdt == before.free_usdt and after.quantity == 0


@pytest.mark.asyncio
async def test_candidates_respect_cap_and_proportion_is_not_nominal_amount(assembled):
    svc, clock, _, _ = assembled
    run = await svc.configure(
        TradingLimits(**(settings_data() | {"leverage": 2, "max_position_notional": "500"})),
        parameter_policy(),
        session_id="s1",
        style_revision=1,
    )
    a = await svc.backend.account(run.scope, clock.utcnow())
    plans = importlib.import_module("agent_platform.domain.trading_plans").build_plans(
        run, a, (await svc.maintenance.market.snapshot("ETHUSDT")).quote
    )
    assert [p.candidate_id for p in plans] == ["WAIT"]
    # Configured choices 5/10 must not silently become an unauthorized 2x plan.
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=a.revision,
        style_revision=1,
        trader_revision=1,
    )
    cycle = await svc.step()
    assert cycle.status == "rejected" and cycle.reason == "no_feasible_plan"
    assert cycle.usage is None and svc.model.calls == 0


@pytest.mark.asyncio
async def test_default_joint_menu_and_same_command_retry_preserve_single_fill(assembled):
    svc, clock, _, _ = assembled
    policy = TradingPolicy(
        order_notional_usdt="100",
        max_price_drift_bps="20",
        min_confidence="0.8",
        strategy_instructions="Choose a bounded complete plan.",
        decision_mode="parameterized",
    )
    run = await svc.configure(
        TradingLimits(**(settings_data() | {"max_leverage": 10})),
        policy,
        session_id="s1",
        style_revision=1,
    )
    a = await svc.backend.account(run.scope, clock.utcnow())
    plans = importlib.import_module("agent_platform.domain.trading_plans").build_plans(
        run, a, (await svc.maintenance.market.snapshot("ETHUSDT")).quote
    )
    assert len(plans) > 10
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=a.revision,
        style_revision=1,
        trader_revision=1,
    )
    svc.model.choices = ("OPEN_LONG_M20_L10",)
    cycle = await svc.step()
    assert cycle.status == "filled", cycle.model_dump()
    record = await svc.execution.journal.get(cycle.command_id)
    retried = await svc.execution.submit(record.command)
    assert retried.receipt.filled_quantity == 1
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.quantity == 1 and a.fees_usdt == 1
    # The original confirmed configuration is idempotent even after current
    # leverage changes; it must neither recharge nor revert the live position.
    assert await svc.configure(run.limits, run.policy, session_id="s1", style_revision=1) == run
    after = await svc.backend.account(run.scope, clock.utcnow())
    assert after.quantity == 1 and after.leverage == 10 and after.fees_usdt == 1
    with pytest.raises(ValidationError, match="evidence"):
        type(record.command).model_validate(record.command.model_dump() | {"target_leverage": 5})
    changed = type(record.command).model_validate(
        record.command.model_dump() | {"target_leverage": 5, "decision_evidence": None}
    )
    with pytest.raises(EventIdentityConflict):
        await svc.execution.submit(changed)


@pytest.mark.parametrize(
    "change",
    [
        {"entry_margin_percents": (0,)},
        {"position_change_percents": (101,)},
        {"leverage_choices": (True,)},
        {"leverage_choices": (5, 5)},
    ],
)
def test_invalid_parameter_configuration_is_rejected(change):
    values = (
        dict(
            order_notional_usdt="100",
            max_price_drift_bps="20",
            min_confidence="0.8",
            strategy_instructions="A bounded strategy.",
            decision_mode="parameterized",
        )
        | change
    )
    with pytest.raises(ValidationError):
        TradingPolicy(**values)
