"""USDT isolated simulation calculations and rejection boundaries, with no network."""

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def modules():
    return (
        importlib.import_module("agent_platform.domain.futures_paper"),
        importlib.import_module("agent_platform.domain.futures_paper_engine"),
    )


def settings_data():
    return dict(
        initial_usdt="1000",
        leverage=5,
        max_position_notional="5000",
        max_run_loss_usdt="100",
        fee_bps="5",
        slippage_bps="0",
    )


def rules_data():
    return dict(
        qty_step="0.001",
        min_qty="0.001",
        max_qty="1000",
        tick_size="0.01",
        min_notional="5",
        maintenance_margin_rate="0.01",
        liquidation_fee_bps="50",
    )


def state():
    d, e = modules()
    initial = e.create_account(
        "s1",
        "ETHUSDT",
        d.FuturesPaperSettings(**settings_data()),
        d.FuturesPaperRules(**rules_data()),
        NOW,
    )
    return d.FuturesPaperState.model_validate(
        initial.model_dump() | {"status": "running", "revision": 2}
    )


def quote(price="2000", *, symbol="ETHUSDT", at=NOW, bid=None, ask=None):
    d, _ = modules()
    return d.FuturesPaperQuote(
        symbol=symbol,
        bid=bid or price,
        ask=ask or price,
        mark=price,
        book_at=at,
        mark_at=at,
        received_at=at,
        source="offline_replay",
    )


def order(action="open_long", quantity="1"):
    d, _ = modules()
    return d.FuturesPaperOrder(action=action, quantity=quantity)


def test_initial_identity_is_usdt_paused_without_spot_balances():
    d, e = modules()
    s = e.create_account(
        "s1",
        "ETHUSDT",
        d.FuturesPaperSettings(**settings_data()),
        d.FuturesPaperRules(**rules_data()),
        NOW,
    )
    assert s.account_ref == "paper:futures:s1"
    assert s.market == "usdt_perpetual" and s.currency == "USDT"
    assert s.status == "paused" and s.free_usdt == 1000 and s.margin_usdt == 0
    assert s.quantity == 0 and s.side is None
    assert "btc" not in s.model_dump()
    with pytest.raises(ValidationError):
        d.FuturesPaperState.model_validate(s.model_dump() | {"account_ref": "paper:s1"})


@pytest.mark.parametrize(
    "field,bad",
    [
        ("leverage", True),
        ("leverage", 21),
        ("leverage", 0),
        ("initial_usdt", 1.2),
        ("initial_usdt", "NaN"),
        ("initial_usdt", "1e1000000"),
        ("initial_usdt", "1e-1000000"),
        ("max_run_loss_usdt", "1001"),
        ("fee_bps", "10000"),
    ],
)
def test_invalid_settings_are_rejected(field, bad):
    d, _ = modules()
    with pytest.raises(ValidationError):
        d.FuturesPaperSettings(**(settings_data() | {field: bad}))


def test_long_partial_and_full_close_preserve_exact_virtual_cash():
    _, e = modules()
    opened = e.execute(state(), order(), quote(), NOW)
    assert opened.state.free_usdt == 599 and opened.state.margin_usdt == 400
    assert opened.state.quantity == 1 and opened.state.side == "long"
    assert e.value(opened.state, quote(), NOW).equity_usdt == 999
    at = NOW + timedelta(seconds=1)
    partial = e.execute(opened.state, order("reduce", "0.4"), quote("2100", at=at), at)
    assert partial.state.free_usdt == Decimal("798.58")
    assert partial.state.margin_usdt == 240 and partial.state.quantity == Decimal("0.6")
    assert partial.state.realized_pnl_usdt == 40 and partial.state.entry_notional == 1200
    closed = e.execute(partial.state, order("reduce", "0.6"), quote("2100", at=at), at)
    assert closed.state.free_usdt == Decimal("1097.95")
    assert closed.state.margin_usdt == closed.state.quantity == closed.state.entry_notional == 0
    assert closed.state.side is None and closed.state.realized_pnl_usdt == 100
    assert closed.state.fees_usdt == Decimal("2.05")


def test_short_buyback_and_mark_pnl_use_opposite_direction():
    _, e = modules()
    short = e.execute(state(), order("open_short"), quote(), NOW)
    at = NOW + timedelta(seconds=1)
    assert e.value(short.state, quote("1900", at=at), at).unrealized_pnl_usdt == 100
    closed = e.execute(short.state, order("reduce"), quote("1900", at=at), at)
    assert closed.operation.side == "buy" and closed.operation.realized_pnl_usdt == 100
    assert closed.state.free_usdt == Decimal("1098.05")


def test_same_direction_add_accumulates_entry_cost_and_margin():
    _, e = modules()
    first = e.execute(state(), order(), quote(), NOW).state
    at = NOW + timedelta(seconds=1)
    added = e.execute(first, order(), quote("2200", at=at), at).state
    assert added.entry_notional == 4200 and added.quantity == 2
    assert added.margin_usdt == 840 and added.free_usdt == Decimal("157.9")
    later = at + timedelta(seconds=1)
    assert e.value(added, quote("2100", at=later), later).unrealized_pnl_usdt == 0


@pytest.mark.parametrize(
    "action,qty", [("open_short", "1"), ("reduce", "2"), ("open_long", "0.0001")]
)
def test_reversal_over_reduction_and_invalid_step_reject(action, qty):
    _, e = modules()
    initial = e.execute(state(), order(), quote(), NOW).state
    with pytest.raises(ValueError):
        e.execute(initial, order(action, qty), quote(), NOW)
    assert initial.free_usdt == 599 and initial.quantity == 1


def test_adverse_slippage_and_tick_rounding_are_applied_to_book():
    d, e = modules()
    s = d.FuturesPaperState.model_validate(
        state().model_dump() | {"settings": settings_data() | {"slippage_bps": "2"}}
    )
    buy = e.execute(s, order(), quote(bid="1999.99", ask="2000.01"), NOW)
    sell = e.execute(s, order("open_short"), quote(bid="1999.99", ask="2000.01"), NOW)
    assert buy.operation.price == Decimal("2000.42")
    assert sell.operation.price == Decimal("1999.59")


@pytest.mark.parametrize("kind", ["stale", "future", "wrong_symbol", "stale_mark"])
def test_unusable_quote_never_opens_position(kind):
    d, e = modules()
    q = quote()
    at = NOW
    if kind == "stale":
        at += timedelta(seconds=6)
    elif kind == "future":
        q = quote(at=NOW + timedelta(seconds=1))
    elif kind == "wrong_symbol":
        q = quote(symbol="BTCUSDT")
    else:
        q = d.FuturesPaperQuote.model_validate(
            q.model_dump() | {"mark_at": NOW - timedelta(seconds=6)}
        )
    with pytest.raises(ValueError):
        e.execute(state(), order(), q, at)


def test_crossed_book_and_maintenance_above_initial_margin_reject():
    d, e = modules()
    with pytest.raises(ValidationError):
        quote(bid="2001", ask="2000")
    r = d.FuturesPaperRules(**(rules_data() | {"maintenance_margin_rate": "0.3"}))
    with pytest.raises(ValueError):
        e.create_account("s1", "ETHUSDT", d.FuturesPaperSettings(**settings_data()), r, NOW)


def test_risk_limits_and_insufficient_cash_reject_increases_but_allow_reduction():
    d, e = modules()
    limited = d.FuturesPaperState.model_validate(
        state().model_dump() | {"settings": settings_data() | {"max_position_notional": "1999"}}
    )
    with pytest.raises(ValueError):
        e.execute(limited, order(), quote(), NOW)
    with pytest.raises(ValueError):
        e.execute(state(), order("open_long", "3"), quote(), NOW)
    opened = e.execute(state(), order(), quote(), NOW).state
    at = NOW + timedelta(seconds=1)
    assert e.value(opened, quote("1880", at=at), at).equity_usdt == 879
    with pytest.raises(ValueError):
        e.execute(opened, order(), quote("1880", at=at), at)
    assert e.execute(
        opened, order("reduce", "0.1"), quote("1880", at=at), at
    ).state.quantity == Decimal("0.9")


def test_liquidation_caps_gap_loss_to_isolated_margin_and_records_shortfall():
    _, e = modules()
    opened = e.execute(state(), order(), quote(), NOW).state
    at = NOW + timedelta(seconds=1)
    liquidated = e.execute(opened, order("reduce"), quote("1400", at=at), at)
    assert liquidated.operation.kind == "liquidation"
    assert liquidated.state.status == "liquidated"
    assert liquidated.state.free_usdt == 599 and liquidated.state.margin_usdt == 0
    assert liquidated.state.quantity == 0 and liquidated.state.realized_pnl_usdt == -400
    assert liquidated.state.shortfall_usdt == 200
    with pytest.raises(ValueError):
        e.execute(liquidated.state, order(), quote(), NOW)


def funding(rate="0.001", at=NOW + timedelta(seconds=10)):
    d, _ = modules()
    return d.FuturesPaperFunding(
        symbol="ETHUSDT", rate=rate, mark="2000", settled_at=at, source="offline_replay"
    )


@pytest.mark.parametrize(
    "action,rate,expected",
    [("open_long", "0.001", "398"), ("open_short", "0.001", "402"), ("open_long", "-0.001", "402")],
)
def test_settled_funding_direction_and_isolated_wallet(action, rate, expected):
    _, e = modules()
    opened = e.execute(state(), order(action), quote(), NOW).state
    f = funding(rate)
    result = e.settle_funding(opened, f, f.settled_at)
    assert result.state.free_usdt == 599 and result.state.margin_usdt == Decimal(expected)
    assert result.state.funding_usdt == Decimal(expected) - 400
    with pytest.raises(ValueError):
        e.settle_funding(result.state, f, f.settled_at)


def test_late_funding_cannot_be_applied_to_a_different_position_version():
    _, e = modules()
    later = NOW + timedelta(seconds=20)
    opened = e.execute(state(), order(), quote(at=later), later).state
    with pytest.raises(ValueError):
        e.settle_funding(opened, funding(), later)


def test_funding_can_trigger_deterministic_liquidation_without_touching_free_cash():
    d, e = modules()
    s = d.FuturesPaperState.model_validate(
        state().model_dump() | {"settings": settings_data() | {"leverage": 20}}
    )
    opened = e.execute(s, order(), quote(), NOW).state
    f = funding("0.1")
    result = e.settle_funding(opened, f, f.settled_at)
    assert result.state.status == "liquidated" and result.state.quantity == 0
    assert result.state.free_usdt == 899 and result.state.shortfall_usdt == 100
    assert result.state.funding_usdt == -100


def test_low_external_decimal_precision_does_not_change_balances():
    _, e = modules()
    expected = e.execute(state(), order(), quote(), NOW)
    with localcontext() as context:
        context.prec = 3
        actual = e.execute(state(), order(), quote(), NOW)
    assert actual == expected


def test_model_copy_cannot_bypass_risk_policy_validation():
    _, e = modules()
    s = state()
    forged = s.model_copy(update={"settings": s.settings.model_copy(update={"leverage": 1000})})
    with pytest.raises(ValueError):
        e.execute(forged, order(), quote(), NOW)


def test_opening_loss_including_spread_cannot_exceed_run_budget():
    d, e = modules()
    s = d.FuturesPaperState.model_validate(
        state().model_dump() | {"settings": settings_data() | {"max_run_loss_usdt": "10"}}
    )
    with pytest.raises(ValueError):
        e.execute(s, order(), quote(bid="2099", ask="2100"), NOW)


def test_flat_paused_wallet_can_record_funding_watermark_without_payment():
    d, e = modules()
    s = e.create_account(
        "s1",
        "ETHUSDT",
        d.FuturesPaperSettings(**settings_data()),
        d.FuturesPaperRules(**rules_data()),
        NOW,
    )
    f = funding()
    result = e.settle_funding(s, f, f.settled_at)
    assert result.state.last_funding_at == f.settled_at
    assert result.state.free_usdt == 1000 and result.state.funding_usdt == 0
    assert result.state.revision == 2 and result.state.status == "paused"


def test_arithmetic_ignores_host_exponent_limits_and_inexact_traps():
    from decimal import Inexact

    _, e = modules()
    s, o, q = state(), order(), quote()
    expected = e.execute(s, o, q, NOW)
    with localcontext() as context:
        context.prec = 3
        context.Emax = 2
        context.traps[Inexact] = True
        actual = e.execute(s, o, q, NOW)
    assert actual == expected


def test_delayed_funding_after_pause_uses_unchanged_position_not_pause_time():
    d, e = modules()
    opened = e.execute(state(), order(), quote(), NOW).state
    paused_at = NOW + timedelta(seconds=20)
    paused = d.FuturesPaperState.model_validate(
        opened.model_dump()
        | {"status": "paused", "updated_at": paused_at, "revision": opened.revision + 1}
    )
    result = e.settle_funding(paused, funding(), paused_at)
    assert result.state.margin_usdt == 398 and result.state.status == "paused"


@pytest.mark.parametrize("change", ["open", "close"])
def test_funding_must_precede_a_position_change_at_the_same_instant(change):
    _, e = modules()
    at = NOW + timedelta(seconds=10)
    opened = (
        e.execute(state(), order(), quote(at=at), at).state
        if change == "open"
        else e.execute(state(), order(), quote(), NOW).state
    )
    changed = (
        opened if change == "open" else e.execute(opened, order("reduce"), quote(at=at), at).state
    )
    with pytest.raises(ValueError):
        e.settle_funding(changed, funding(at=at), at)


def test_funding_exhaustion_liquidates_and_settles_debt_from_position_proceeds():
    d, e = modules()
    s = d.FuturesPaperState.model_validate(
        state().model_dump() | {"settings": settings_data() | {"leverage": 20}}
    )
    opened = e.execute(s, order(), quote(), NOW).state
    f = d.FuturesPaperFunding.model_validate(funding("0.1").model_dump() | {"mark": "3000"})
    result = e.settle_funding(opened, f, f.settled_at)
    assert result.state.status == "liquidated" and result.state.quantity == 0
    assert result.state.funding_usdt == -300 and result.state.shortfall_usdt == 0
    assert result.state.free_usdt == 1684


@pytest.mark.parametrize("kind", ["older", "conflicting_timestamp"])
def test_consumed_quote_watermark_rejects_old_or_conflicting_evidence(kind):
    _, e = modules()
    at = NOW + timedelta(seconds=1)
    opened = e.execute(state(), order(), quote("2100", at=at), at).state
    old = quote("2200", at=NOW if kind == "older" else at)
    with pytest.raises(ValueError):
        e.execute(opened, order("reduce"), old, at + timedelta(seconds=1))


def test_equivalent_trailing_zero_quantity_has_identical_execution():
    _, e = modules()
    expected = e.execute(state(), order(), quote("2000.13"), NOW)
    actual = e.execute(state(), order(quantity="1.000000000000000000000000"), quote("2000.13"), NOW)
    assert actual == expected


def test_fine_contract_precision_quantizes_entry_cost_instead_of_rejecting_product():
    d, e = modules()
    s = d.FuturesPaperState.model_validate(
        state().model_dump()
        | {
            "rules": rules_data()
            | {"tick_size": "1e-12", "qty_step": "1e-13", "min_qty": "1e-13", "min_notional": "0"}
        }
    )
    qty, price = "0.1234567890123", "1.123456789012"
    result = e.execute(s, order(quantity=qty), quote(price), NOW)
    assert result.state.entry_notional == (Decimal(qty) * Decimal(price)).quantize(Decimal("1e-24"))
