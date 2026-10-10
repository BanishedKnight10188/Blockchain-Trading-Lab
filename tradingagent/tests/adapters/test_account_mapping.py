"""Wire values remain exact and never imply execution permission or known cost."""

import importlib
import json
from decimal import Decimal, localcontext

import pytest

from agent_platform.domain.account import ObservedTrade
from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS


def module():
    return importlib.import_module("agent_platform.adapters.binance_direct.account_mapping")


def account_wire():
    return {
        "accountType": "SPOT",
        "canTrade": True,
        "canWithdraw": True,
        "updateTime": 0,
        "balances": [
            {"asset": "BTC", "free": "0.123456789123456789", "locked": "0.000000000000000001"},
            {"asset": "USDT", "free": "500.1", "locked": "0"},
        ],
    }


def order_wire(**changes):
    return {
        "symbol": "BTCUSDT",
        "orderId": 123,
        "side": "BUY",
        "status": "PARTIALLY_FILLED",
        "origQty": "0.002",
        "executedQty": "0.001",
        "price": "60000.00000001",
        "time": MS - 10000,
        "updateTime": MS - 1000,
        **changes,
    }


def trade_wire(trade_id=123, **changes):
    return {
        "symbol": "BTCUSDT",
        "id": trade_id,
        "orderId": 456,
        "price": "60000.00000001",
        "qty": "0.001",
        "quoteQty": "60.00000000",
        "commission": "0.000000123456789123456789",
        "commissionAsset": "BNB",
        "time": MS - 1000,
        "isBuyer": True,
        "isMaker": False,
        **changes,
    }


def test_account_sampling_time_is_receipt_not_last_mutation_and_exact_balances():
    with localcontext() as context:
        context.prec = 5
        result = module().map_account(account_wire(), "account-1", NOW)
        assert result.balances[0].total == Decimal("0.123456789123456790")
    assert result.as_of == NOW
    assert result.status == "fresh" and result.account_revision == 0
    assert "canTrade" not in result.model_dump() and "canWithdraw" not in result.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"accountType": "MARGIN"},
        {"balances": {}},
        {"balances": [{"asset": "BTC", "free": 1.5, "locked": "0"}]},
        {"balances": [{"asset": "BTC", "free": "1e100000", "locked": "0"}]},
        {"balances": [{"asset": "BTC", "free": "1", "locked": "0"}] * 2},
    ],
)
def test_invalid_account_shape_or_amounts_are_sanitized(changes):
    wire = {**account_wire(), **changes, "msg": "private-marker"}
    with pytest.raises(module().AccountMappingError) as captured:
        module().map_account(wire, "account-1", NOW)
    assert "private-marker" not in str(captured.value)


@pytest.mark.parametrize(
    "status,filled,expected",
    [
        ("NEW", "0", "new"),
        ("PARTIALLY_FILLED", "0.001", "partially_filled"),
        ("FILLED", "0.002", "filled"),
        ("CANCELED", "0.001", "canceled"),
        ("EXPIRED_IN_MATCH", "0", "expired"),
    ],
)
def test_order_status_and_market_zero_price_are_owned_facts(status, filled, expected):
    result = module().map_order(
        order_wire(status=status, executedQty=filled, price="0"), "account-1"
    )
    assert result.order_id == "123" and result.side == "buy"
    assert result.status == expected and result.price == 0
    assert result.updated_at.timestamp() * 1000 == MS - 1000


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETHUSDT"},
        {"executedQty": "0.003"},
        {"orderId": True},
        {"side": "SHORT"},
        {"status": "UNKNOWN"},
    ],
)
def test_invalid_order_scope_or_inconsistent_fill_is_rejected(changes):
    with pytest.raises(module().AccountMappingError):
        module().map_order(order_wire(**changes), "account-1")


def test_trade_retains_actual_quote_value_fee_asset_and_human_executor():
    result = module().map_trade(trade_wire(isBuyer=False), "account-1")
    assert result.trade_id == "123" and result.order_id == "456"
    assert result.side == "sell" and result.executor == "human"
    assert result.quote_quantity == Decimal("60.00000000")
    assert result.fee_asset == "BNB" and result.fee == Decimal("0.000000123456789123456789")
    assert result.quote_quantity != result.price * result.quantity


@pytest.mark.parametrize(
    "changes",
    [
        {"isBuyer": "false"},
        {"isBuyer": 0},
        {"id": -1},
        {"id": 2**63},
        {"qty": "0"},
        {"commission": "-0.01"},
        {"quoteQty": 60.0},
    ],
)
def test_trade_wire_boolean_identity_and_amounts_are_strict(changes):
    with pytest.raises(module().AccountMappingError):
        module().map_trade(trade_wire(**changes), "account-1")


def test_legacy_trade_json_without_quote_is_still_readable_and_roundtrips():
    result = module().map_trade(trade_wire(), "account-1")
    old = result.model_dump(mode="json")
    del old["quote_quantity"]
    assert ObservedTrade.model_validate_json(json.dumps(old)).quote_quantity is None
    assert ObservedTrade.model_validate_json(result.model_dump_json()) == result


def test_paper_account_cannot_receive_real_observations():
    with pytest.raises(module().AccountMappingError):
        module().map_trade(
            trade_wire(),
            "paper:account",
        )
