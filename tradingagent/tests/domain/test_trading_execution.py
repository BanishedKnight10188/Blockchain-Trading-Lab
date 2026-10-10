"""Neutral futures contracts reject mixed identities and impossible execution facts."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.domain.test_futures_paper import NOW, quote


def module():
    return importlib.import_module("agent_platform.domain.trading_execution")


def scope_data(**changes):
    return (
        dict(environment="paper", account_ref="paper:futures:s1", session_id="s1", symbol="ETHUSDT")
        | changes
    )


def command_data(**changes):
    return (
        dict(
            command_id="order-1",
            scope=scope_data(),
            action="open_long",
            quantity="1",
            created_at=NOW,
            expires_at=NOW + timedelta(seconds=5),
            expected_account_revision=2,
            style_revision=1,
            trader_revision=1,
        )
        | changes
    )


def command(**changes):
    return module().TradeCommand(**command_data(**changes))


def receipt_data(**changes):
    return (
        dict(
            command=command(),
            status="filled",
            filled_quantity="1",
            average_price="2000",
            fee_usdt="1",
            backend_order_id="paper-order:order-1",
            backend_at=NOW,
            observed_at=NOW,
        )
        | changes
    )


def test_neutral_public_quote_and_legacy_quote_share_validated_values():
    values = importlib.import_module("agent_platform.domain.futures_values")
    q = values.FuturesQuote.model_validate(quote().model_dump())
    assert q.bid == 2000 and q.symbol == "ETHUSDT"
    assert isinstance(quote(), values.FuturesQuote)
    with pytest.raises(ValueError):
        values.FuturesQuote.model_validate(q.model_copy(update={"ask": "0"}))


@pytest.mark.parametrize(
    "changes",
    [
        dict(environment="live"),
        dict(account_ref="real:1"),
        dict(account_ref="paper:futures:other"),
        dict(environment="testnet"),
        dict(symbol="ETHBTC"),
        dict(session_id=""),
    ],
)
def test_scope_rejects_live_mixed_namespace_and_non_usdt(changes):
    with pytest.raises(ValueError):
        module().ExecutionScope(**scope_data(**changes))


def test_testnet_scope_is_an_identity_without_execution_capability():
    s = module().ExecutionScope(
        **scope_data(environment="testnet", account_ref="testnet:futures:alice")
    )
    assert s.environment == "testnet" and s.symbol == "ETHUSDT"


@pytest.mark.parametrize(
    "changes",
    [
        dict(expires_at=NOW),
        dict(expires_at=NOW + timedelta(seconds=31)),
        dict(quantity="0"),
        dict(quantity=True),
        dict(style_revision=True),
        dict(command_id="x" * 129),
        dict(action="withdraw"),
        dict(expected_account_revision=0),
    ],
)
def test_command_rejects_invalid_execution_inputs(changes):
    with pytest.raises(ValueError):
        command(**changes)


def test_command_revalidates_unsafe_nested_scope_and_own_copy():
    d = module()
    s = d.ExecutionScope(**scope_data()).model_copy(update={"environment": "live"})
    with pytest.raises(ValueError):
        command(scope=s)
    with pytest.raises(ValueError):
        d.TradeCommand.model_validate(command().model_copy(update={"quantity": "0"}))


@pytest.mark.parametrize(
    "changes",
    [
        dict(filled_quantity="0"),
        dict(filled_quantity="2"),
        dict(average_price=None),
        dict(status="accepted"),
        dict(status="partially_filled"),
        dict(status="rejected"),
        dict(observed_at=NOW - timedelta(seconds=1)),
    ],
)
def test_receipt_rejects_impossible_or_backdated_fill(changes):
    with pytest.raises(ValueError):
        module().ExecutionReceipt(**receipt_data(**changes))


@pytest.mark.parametrize("status", ["pending", "accepted", "unknown", "rejected", "canceled"])
def test_unfilled_receipt_never_invents_price_or_fees(status):
    d = module()
    r = d.ExecutionReceipt(
        **receipt_data(status=status, filled_quantity="0", average_price=None, fee_usdt="0")
    )
    assert r.filled_quantity == 0 and r.average_price is None
    with pytest.raises(ValueError):
        d.ExecutionReceipt.model_validate(r.model_dump() | {"fee_usdt": "1"})


def test_partial_and_canceled_receipts_preserve_known_execution():
    d = module()
    r = d.ExecutionReceipt(
        **receipt_data(status="partially_filled", filled_quantity="0.4", fee_usdt="0.4")
    )
    c = d.ExecutionReceipt.model_validate(r.model_dump() | {"status": "canceled"})
    assert c.filled_quantity == Decimal("0.4") and c.fee_usdt == Decimal("0.4")


def test_record_rejects_receipt_for_another_command_and_unsafe_copy():
    d = module()
    r = d.ExecutionReceipt(**receipt_data())
    with pytest.raises(ValueError):
        d.ExecutionRecord(command=command(command_id="other"), receipt=r, revision=1)
    with pytest.raises(ValueError):
        d.ExecutionRecord(
            command=command(), receipt=r.model_copy(update={"filled_quantity": "2"}), revision=1
        )


def test_account_without_quote_reports_unknown_valuation():
    d = module()
    a = d.TradingAccountSnapshot(
        scope=d.ExecutionScope(**scope_data()),
        revision=2,
        status="running",
        free_usdt="1000",
        margin_usdt="0",
        quantity="0",
        side=None,
        entry_notional="0",
        realized_pnl_usdt="0",
        funding_usdt="0",
        fees_usdt="0",
        captured_at=NOW,
    )
    assert a.equity_usdt is None and a.unrealized_pnl_usdt is None
    with pytest.raises(ValueError):
        d.TradingAccountSnapshot.model_validate(a.model_dump() | {"side": "long"})
    with pytest.raises(ValueError):
        d.TradingAccountSnapshot.model_validate(a.model_dump() | {"equity_usdt": "1000"})
