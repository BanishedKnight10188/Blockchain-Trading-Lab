"""Imported exchange facts cannot silently regress or invent position costs."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def account():
    return importlib.import_module("agent_platform.domain.account")


def order(account, **changes):
    values = {
        "account_ref": "personal-spot",
        "symbol": "BTCUSDT",
        "order_id": "123",
        "side": "buy",
        "status": "new",
        "quantity": "0.2",
        "filled_quantity": "0",
        "price": "60000",
        "updated_at": datetime(2026, 10, 5, tzinfo=UTC),
    }
    return account.ObservedOrder(**{**values, **changes})


@pytest.mark.parametrize("status", ["filled", "canceled", "rejected", "expired"])
def test_terminal_order_cannot_regress(account, status):
    terminal = order(account, status=status, filled_quantity="0.2" if status == "filled" else "0")
    with pytest.raises(ValueError):
        terminal.apply_observation(order(account, status="new"))


def test_cumulative_fill_cannot_decrease(account):
    original = order(account, status="partially_filled", filled_quantity="0.1")
    with pytest.raises(ValueError):
        original.apply_observation(
            order(
                account,
                status="partially_filled",
                filled_quantity="0.01",
            )
        )


def test_new_order_cannot_have_a_nonzero_fill(account):
    with pytest.raises(ValueError):
        order(account, status="new", filled_quantity="0.1")


def test_partially_filled_state_cannot_regress_to_new(account):
    original = order(account, status="partially_filled", filled_quantity="0.1")
    regressed = original.model_copy(
        update={
            "status": account.OrderStatus.NEW,
            "updated_at": original.updated_at + timedelta(seconds=1),
        }
    )
    with pytest.raises(ValueError):
        original.apply_observation(regressed)


def test_progression_preserves_exact_quantity(account):
    original = order(account)
    changed = original.apply_observation(
        order(
            account,
            status="filled",
            filled_quantity="0.2",
            updated_at=original.updated_at + timedelta(seconds=1),
        )
    )
    assert str(changed.filled_quantity) == "0.2"
    assert original.filled_quantity == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"filled_quantity": "0.3"},
        {"status": "filled", "filled_quantity": "0.1"},
        {"market_type": "unsupported"},
        {"quantity": 0.1},
    ],
)
def test_inconsistent_order_is_rejected(account, changes):
    with pytest.raises(ValueError):
        order(account, **changes)


def test_observation_from_another_account_or_older_time_is_rejected(account):
    original = order(account)
    with pytest.raises(ValueError):
        original.apply_observation(order(account, account_ref="other-account"))
    with pytest.raises(ValueError):
        original.apply_observation(
            order(
                account,
                updated_at=original.updated_at - timedelta(seconds=1),
            )
        )


def test_unknown_cost_is_absent_instead_of_zero(account):
    position = account.PositionView(symbol="BTCUSDT", quantity="0.5", cost_status="unknown")
    assert position.average_cost is None
    assert position.total_cost is None
    with pytest.raises(ValueError):
        account.PositionView(
            symbol="BTCUSDT",
            quantity="0.5",
            cost_status="unknown",
            total_cost="0",
        )


def test_snapshot_rejects_duplicate_balances(account):
    with pytest.raises(ValueError):
        account.AccountSnapshot(
            account_ref="personal-spot",
            as_of=datetime(2026, 10, 5, tzinfo=UTC),
            balances=({"asset": "BTC", "free": "1", "locked": "0"},) * 2,
        )
