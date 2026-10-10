"""Hard discipline is deterministic and identical at style 0 and 100."""

import importlib
from datetime import timedelta
from decimal import ROUND_DOWN, Inexact, localcontext

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.decisions import AdvisoryAssessment, DecisionSnapshot
from tests.domain.test_decisions import NOW, snapshot_data


def module():
    return importlib.import_module("agent_platform.application.risk")


def snapshot(
    *,
    strength=50,
    quantity="0.02",
    free_btc="0.01",
    locked_btc="0.01",
    free_usdt="2000",
    limits=None,
    quote_age=0,
    account_age=0,
    feature_age=0,
):
    data = snapshot_data()
    data["style"] = {"strength": strength}
    data["market"].update(
        status="ready",
        latest_received_at=NOW,
        latest_quote_at=NOW - timedelta(seconds=quote_age),
        book_as_of=NOW - timedelta(seconds=quote_age),
        book={
            "symbol": "BTCUSDT",
            "bid": "59999",
            "ask": "60000",
            "bid_quantity": "1",
            "ask_quantity": "1",
        },
    )
    data["features"].update(
        as_of=NOW - timedelta(seconds=feature_age),
        warmup_ready=True,
        interval_return="0",
        ema_fast="60000",
        ema_slow="60000",
        atr="1",
        vwap="60000",
        volatility="0",
        volume_change="0",
        spread="1",
    )
    data["account"].update(
        as_of=NOW - timedelta(seconds=account_age),
        status="fresh",
        account_revision=1,
        balances=[
            {"asset": "BTC", "free": free_btc, "locked": locked_btc},
            {"asset": "USDT", "free": free_usdt, "locked": "0"},
        ],
    )
    data["position"]["quantity"] = quantity
    data["limits"] = limits or {}
    return DecisionSnapshot(**data)


def advice(action="buy", quantity=None, **changes):
    values = {
        "action": action,
        "quantity": quantity,
        "explanation": "明确的测试输入",
        "source": "fake",
        "evidence_ids": ("features-1",) if action in ("buy", "sell") else (),
    }
    return AdvisoryAssessment(**{**values, **changes})


def context(**changes):
    return module().RiskContext(
        instrument={
            "symbol": "BTCUSDT",
            "base_asset": "BTC",
            "quote_asset": "USDT",
            "filter_version": "fixture-filter-v1",
            "price_tick": "0.01",
            "quantity_step": "0.001",
            "min_quantity": "0.001",
            "max_quantity": "1",
            "min_notional": "10",
        },
        cost_buffer_rate="0.01",
        cost_policy_version="test-buffer-v1",
        **changes,
    )


@pytest.mark.parametrize("strength", [0, 100])
def test_directional_advice_without_quantity_can_pass_without_invented_limits(strength):
    result = module().RiskService(FakeClock(NOW)).evaluate(snapshot(strength=strength), advice())
    assert result.outcome == "allow" and not result.reasons
    assert advice().quantity is None


@pytest.mark.parametrize("strength", [0, 100])
def test_missing_user_quantity_limit_is_blocked_for_both_styles(strength):
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(snapshot(strength=strength), advice(quantity="0.001"))
    )
    assert result.outcome == "block" and "quantity_limit_unconfigured" in result.reasons


@pytest.mark.parametrize("strength", [0, 100])
def test_user_caps_and_existing_position_cannot_be_bypassed_by_style(strength):
    limits = {"max_buy_quantity": "0.002", "max_position_quantity": "0.021"}
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(
            snapshot(strength=strength, limits=limits), advice(quantity="0.003"), context=context()
        )
    )
    assert result.outcome == "block"
    assert {"quantity_limit_exceeded", "position_limit_exceeded"} <= set(result.reasons)


@pytest.mark.parametrize("age,expected", [(5, "allow"), (6, "unavailable")])
def test_quote_age_boundary_uses_quote_time_not_snapshot_publication_time(age, expected):
    result = module().RiskService(FakeClock(NOW)).evaluate(snapshot(quote_age=age), advice())
    assert result.outcome == expected


@pytest.mark.parametrize("age,expected", [(60, "allow"), (61, "unavailable")])
def test_account_age_boundary(age, expected):
    result = module().RiskService(FakeClock(NOW)).evaluate(snapshot(account_age=age), advice())
    assert result.outcome == expected


@pytest.mark.parametrize("action", ["buy", "sell", "hold"])
def test_unready_features_or_market_gap_is_not_a_normal_hold(action):
    data = snapshot().model_dump()
    data["market"]["status"] = "gap"
    data["features"]["warmup_ready"] = False
    result = module().RiskService(FakeClock(NOW)).evaluate(DecisionSnapshot(**data), advice(action))
    assert result.outcome == "unavailable"
    assert {"market_not_ready", "features_not_ready"} <= set(result.reasons)


@pytest.mark.parametrize(
    "action,free,locked", [("sell", "0", "0"), ("sell", "0", "0.02"), ("buy", "0.01", "0.01")]
)
def test_no_free_asset_prevents_spot_sell_or_buy(action, free, locked):
    quantity = "0" if free == locked == "0" else "0.02"
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(
            snapshot(quantity=quantity, free_btc=free, locked_btc=locked, free_usdt="0"),
            advice(action),
        )
    )
    assert result.outcome == "block"
    assert "insufficient_free_balance" in result.reasons


def test_position_projection_must_match_confirmed_balance():
    result = module().RiskService(FakeClock(NOW)).evaluate(snapshot(quantity="0.03"), advice())
    assert result.outcome == "unavailable" and "position_balance_mismatch" in result.reasons


def test_numeric_advice_needs_filters_and_explicit_cost_buffer():
    limits = {"max_buy_quantity": "0.01", "max_position_quantity": "0.1"}
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(snapshot(limits=limits), advice(quantity="0.001"))
    )
    assert result.outcome == "unavailable"
    assert {"instrument_unavailable", "cost_buffer_unconfigured"} <= set(result.reasons)


@pytest.mark.parametrize(
    "quantity,reason",
    [
        ("0.0015", "quantity_step_mismatch"),
        ("0.0001", "quantity_below_minimum"),
        ("1.001", "exchange_quantity_exceeded"),
    ],
)
def test_exchange_quantity_filters_are_enforced(quantity, reason):
    limits = {"max_buy_quantity": "2", "max_position_quantity": "3"}
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(
            snapshot(limits=limits, free_usdt="100000"),
            advice(quantity=quantity),
            context=context(),
        )
    )
    assert result.outcome == "block" and reason in result.reasons


def test_exact_affordability_includes_configured_buffer_under_low_decimal_precision():
    limits = {"max_buy_quantity": "0.01", "max_position_quantity": "0.1"}
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        allowed = (
            module()
            .RiskService(FakeClock(NOW))
            .evaluate(
                snapshot(limits=limits, free_usdt="60.60"),
                advice(quantity="0.001"),
                context=context(),
            )
        )
        blocked = (
            module()
            .RiskService(FakeClock(NOW))
            .evaluate(
                snapshot(limits=limits, free_usdt="60.599999"),
                advice(quantity="0.001"),
                context=context(),
            )
        )
    assert allowed.outcome == "allow"
    assert blocked.outcome == "block" and "insufficient_free_balance" in blocked.reasons


def test_configured_daily_loss_requires_complete_current_evidence():
    limits = {"max_daily_loss_usd": "10"}
    result = module().RiskService(FakeClock(NOW)).evaluate(snapshot(limits=limits), advice())
    assert result.outcome == "unavailable" and "daily_loss_unavailable" in result.reasons


@pytest.mark.parametrize(
    "action,expected", [("buy", "block"), ("sell", "allow"), ("hold", "allow")]
)
def test_daily_loss_stops_increasing_risk_without_forcing_a_new_trade(action, expected):
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(
            snapshot(limits={"max_daily_loss_usd": "10"}),
            advice(action),
            context=context(daily_loss_usd="10", daily_loss_as_of=NOW, daily_loss_complete=True),
        )
    )
    assert result.outcome == expected


def test_old_daily_loss_and_unknown_evidence_cannot_release_buy_advice():
    old = context(
        daily_loss_usd="0", daily_loss_as_of=NOW - timedelta(days=1), daily_loss_complete=True
    )
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(
            snapshot(limits={"max_daily_loss_usd": "10"}),
            advice(evidence_ids=("unrecorded",)),
            context=old,
        )
    )
    assert result.outcome == "unavailable"
    assert {"daily_loss_unavailable", "advice_evidence_unknown"} <= set(result.reasons)


def test_expired_trigger_and_unavailable_proposal_cannot_be_published():
    service = module().RiskService(FakeClock(NOW + timedelta(seconds=60)))
    assert "trigger_expired" in service.evaluate(snapshot(), advice()).reasons
    unavailable = AdvisoryAssessment(
        action="unavailable",
        explanation="数据不可用",
        source="rule",
        unavailable_reasons=("provider_unavailable",),
    )
    assert (
        module().RiskService(FakeClock(NOW)).evaluate(snapshot(), unavailable).outcome
        == "unavailable"
    )


def test_extreme_decimal_exponents_are_rejected_without_large_calculation():
    limits = {"max_buy_quantity": "1e999999", "max_position_quantity": "1e999999"}
    result = (
        module()
        .RiskService(FakeClock(NOW))
        .evaluate(snapshot(limits=limits), advice(quantity="1e999998"), context=context())
    )
    assert result.outcome == "unavailable" and "numeric_bounds" in result.reasons


def test_ambient_exponent_clamp_rounding_and_traps_do_not_change_risk_result():
    data = snapshot(limits={"max_buy_quantity": "0.01", "max_position_quantity": "0.1"})
    proposal, inputs = advice(quantity="0.001"), context()
    service = module().RiskService(FakeClock(NOW))
    normal = service.evaluate(data, proposal, context=inputs)
    with localcontext() as decimal_context:
        decimal_context.Emax, decimal_context.Emin = 0, 0
        decimal_context.clamp, decimal_context.rounding = 1, ROUND_DOWN
        decimal_context.traps[Inexact] = True
        assert service.evaluate(data, proposal, context=inputs) == normal
