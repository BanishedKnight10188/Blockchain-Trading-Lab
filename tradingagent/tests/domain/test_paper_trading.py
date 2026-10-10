"""Paper policies and owned identities cannot borrow production balances."""

import importlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

NOW = datetime(2026, 10, 6, tzinfo=UTC)


def settings_data():
    return dict(
        initial_usdt="1000",
        order_quantity="0.001",
        max_position_quantity="0.01",
        max_run_loss_usdt="50",
        fee_bps="10",
        slippage_bps="2",
        max_price_drift_bps="20",
        min_confidence="0.8",
        strategy_instructions="Only buy with sufficient evidence; otherwise wait.",
    )


def paper_module():
    return importlib.import_module("agent_platform.domain.paper_trading")


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_usdt", 1.5),
        ("initial_usdt", "NaN"),
        ("order_quantity", "1e-100"),
        ("fee_bps", "10000"),
        ("min_confidence", "1.01"),
        ("strategy_instructions", " "),
        ("max_run_loss_usdt", "1001"),
    ],
)
def test_strict_policy_rejects_invalid_or_unbounded_money(field, value):
    values = settings_data() | {field: value}
    with pytest.raises(ValidationError):
        paper_module().PaperSettings(**values)


def test_order_cannot_exceed_position_limit():
    with pytest.raises(ValidationError):
        paper_module().PaperSettings(**(settings_data() | {"max_position_quantity": "0.0001"}))


def test_account_identity_and_initial_wallet_are_explicit():
    module = paper_module()
    policy = module.PaperSettings(**settings_data())
    wallet = module.PaperAccountState(
        account_ref="paper:session-1",
        session_id="session-1",
        settings=policy,
        usdt="1000",
        btc="0",
        created_at=NOW,
        updated_at=NOW,
    )
    assert wallet.status == "paused"
    assert wallet.settings.initial_usdt == 1000
    with pytest.raises(ValidationError):
        module.PaperAccountState(**(wallet.model_dump() | {"account_ref": "binance-local"}))
    with pytest.raises(ValidationError):
        module.PaperAccountState(**(wallet.model_dump() | {"session_id": "another-session"}))


def test_paper_is_a_distinct_execution_environment():
    from agent_platform.domain.operating_modes import OperatingSettings

    assert (
        OperatingSettings(mode="auto", execution_environment="paper").execution_environment
        == "paper"
    )
