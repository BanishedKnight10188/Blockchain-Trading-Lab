"""FIFO facts preserve fees, missing basis and exact observed trade identities."""

import importlib
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from agent_platform.domain.account import ObservedTrade, TradeBatch
from tests.domain.test_decisions import NOW


def fill(identity, side, quantity, quote, *, fee="0", asset="USDT", second=0):
    return ObservedTrade(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trade_id=identity,
        order_id="order-" + identity,
        side=side,
        price="60000",
        quantity=quantity,
        quote_quantity=quote,
        fee=fee,
        fee_asset=asset,
        executed_at=NOW + timedelta(seconds=second),
    )


def analyze(trades, *, complete=True, opening="0", ending="0", movements=True, coverage=True):
    domain = importlib.import_module("agent_platform.domain.review_facts")
    application = importlib.import_module("agent_platform.application.trade_groups")
    cutoff = NOW + timedelta(seconds=60)
    proof = (
        domain.InventoryCoverage(
            account_ref="local-spot",
            symbol="BTCUSDT",
            market_type="spot",
            evidence_id="synthetic-inventory-proof",
            period_start=NOW - timedelta(seconds=1),
            as_of=cutoff,
            opening_base_quantity=opening,
            ending_base_quantity=ending,
            movements_complete=movements,
            nontrade_base_movements_abs="0",
        )
        if coverage
        else None
    )
    batch = TradeBatch(
        account_ref="local-spot", symbol="BTCUSDT", trades=tuple(trades), history_complete=complete
    )
    return application.FifoAnalyzer().analyze(batch, cutoff, proof)


def test_partial_buys_and_sells_match_fifo_and_preserve_remaining_cost():
    result = analyze(
        [
            fill("1", "buy", "2", "100", second=0),
            fill("2", "buy", "1", "80", second=1),
            fill("3", "sell", "2.5", "200", second=2),
        ],
        ending="0.5",
    )
    assert result.cost_status == "known" and result.realized_pnl_quote == Decimal("60")
    assert [
        (item.buy_trade_id, item.inventory_quantity, item.cost_quote) for item in result.matches
    ] == [
        ("1", Decimal("2"), Decimal("100")),
        ("2", Decimal("0.5"), Decimal("40")),
    ]
    assert result.remaining_lots[0].quantity == Decimal("0.5")
    assert result.remaining_lots[0].cost_quote == Decimal("40")
    assert result.quote_asset == "USDT" and result.realized_pnl_usd is None


def test_quote_asset_fees_are_added_to_cost_and_subtracted_from_proceeds():
    result = analyze(
        [fill("1", "buy", "1", "100", fee="2"), fill("2", "sell", "1", "110", fee="3", second=1)]
    )
    assert result.realized_pnl_quote == Decimal("5")
    assert result.matches[0].cost_quote == Decimal("102")
    assert result.matches[0].proceeds_quote == Decimal("107")


def test_base_asset_fee_reduces_buy_inventory():
    result = analyze(
        [
            fill("1", "buy", "1", "100", fee="0.01", asset="BTC"),
            fill("2", "sell", "0.99", "110", second=1),
        ]
    )
    assert result.matches[0].inventory_quantity == Decimal("0.99")
    assert result.realized_pnl_quote == Decimal("10")


def test_base_asset_sell_fee_consumes_additional_fifo_inventory():
    result = analyze(
        [
            fill("1", "buy", "1", "100"),
            fill("2", "sell", "0.99", "90", fee="0.01", asset="BTC", second=1),
        ]
    )
    assert result.matches[0].inventory_quantity == Decimal("1")
    assert result.realized_pnl_quote == Decimal("-10") and result.inventory_quantity == 0


@pytest.mark.parametrize(
    "updates", [dict(complete=False), dict(movements=False), dict(coverage=False), dict(ending="1")]
)
def test_incomplete_history_or_inventory_evidence_cannot_claim_profit(updates):
    result = analyze(
        [fill("1", "buy", "1", "100"), fill("2", "sell", "1", "110", second=1)], **updates
    )
    assert result.cost_status != "known" and result.realized_pnl_quote is None
    assert all(item.realized_pnl_quote is None for item in result.matches)
    assert result.reasons


def test_deposited_opening_bitcoin_has_unknown_cost():
    result = analyze([fill("1", "sell", "1", "110")], opening="1")
    assert result.cost_status == "unknown" and result.realized_pnl_quote is None
    assert result.matches[0].buy_trade_id is None and result.matches[0].cost_quote is None


def test_unobserved_opening_inventory_is_not_assigned_zero_cost():
    result = analyze([fill("1", "sell", "1", "110")])
    assert result.unmatched_sold_quantity == 1 and result.realized_pnl_quote is None
    assert "missing_inventory_basis" in result.reasons


@pytest.mark.parametrize("movements", [None, "2"])
def test_net_zero_transfers_without_zero_absolute_movement_proof_cannot_claim_profit(movements):
    domain = importlib.import_module("agent_platform.domain.review_facts")
    application = importlib.import_module("agent_platform.application.trade_groups")
    cutoff = NOW + timedelta(seconds=60)
    batch = TradeBatch(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trades=(fill("1", "buy", "1", "100"), fill("2", "sell", "1", "110", second=1)),
        history_complete=True,
    )
    proof = domain.InventoryCoverage(
        account_ref="local-spot",
        symbol="BTCUSDT",
        evidence_id="round-trip-transfers",
        period_start=NOW,
        as_of=cutoff,
        opening_base_quantity="0",
        ending_base_quantity="0",
        movements_complete=True,
        nontrade_base_movements_abs=movements,
    )
    result = application.FifoAnalyzer().analyze(batch, cutoff, proof)
    assert result.cost_status != "known" and result.realized_pnl_quote is None


def test_inventory_proof_cannot_authorize_another_account_or_market():
    domain = importlib.import_module("agent_platform.domain.review_facts")
    application = importlib.import_module("agent_platform.application.trade_groups")
    cutoff = NOW + timedelta(seconds=60)
    batch = TradeBatch(
        account_ref="local-spot",
        symbol="BTCUSDT",
        trades=(fill("1", "buy", "1", "100"),),
        history_complete=True,
    )
    for updates in (
        dict(account_ref="foreign"),
        dict(symbol="ETHUSDT"),
        dict(market_type="futures"),
    ):
        proof = domain.InventoryCoverage(
            evidence_id="foreign-proof",
            period_start=NOW,
            as_of=cutoff,
            opening_base_quantity="0",
            ending_base_quantity="1",
            movements_complete=True,
            **dict(account_ref="local-spot", symbol="BTCUSDT", market_type="spot") | updates,
        )
        with pytest.raises(ValueError):
            application.FifoAnalyzer().analyze(batch, cutoff, proof)


def test_unmatched_sell_preserves_all_actual_quote_proceeds_as_unknown_cost():
    result = analyze(
        [fill("1", "buy", "1", "100"), fill("2", "sell", "2", "220", second=1)], coverage=False
    )
    assert len(result.matches) == 2 and result.matches[-1].buy_trade_id is None
    assert result.matches[-1].cost_quote is None and result.matches[-1].proceeds_quote == 110
    assert sum(item.proceeds_quote for item in result.matches) == 220
    assert result.realized_pnl_quote is None


def test_maximum_input_with_unknown_opening_lot_remains_bounded_and_supported():
    trades = [fill(str(i + 1), "buy", "1", "100", second=i % 50) for i in range(4096)]
    trades.sort(key=lambda item: (item.executed_at, int(item.trade_id)))
    result = analyze(trades, opening="1", ending="4097")
    assert len(result.remaining_lots) == 4097 and result.inventory_quantity == 4097
    assert result.realized_pnl_quote is None


@pytest.mark.parametrize("fee,quote", [("0.01", "100"), ("0", None)])
def test_unvalued_third_asset_fee_or_missing_actual_quote_keeps_cost_unknown(fee, quote):
    result = analyze(
        [
            fill("1", "buy", "1", quote, fee=fee, asset="BNB"),
            fill("2", "sell", "1", "110", second=1),
        ]
    )
    assert result.cost_status == "unknown" and result.realized_pnl_quote is None
    assert result.matches[0].cost_quote is None


def test_zero_third_asset_fee_needs_no_exchange_rate():
    assert (
        analyze(
            [fill("1", "buy", "1", "100", asset="BNB"), fill("2", "sell", "1", "110", second=1)]
        ).realized_pnl_quote
        == 10
    )


def test_partial_known_and_unknown_lots_do_not_claim_total_pnl():
    result = analyze(
        [
            fill("1", "buy", "1", "100", fee="0.01", asset="BNB"),
            fill("2", "buy", "1", "100", second=1),
            fill("3", "sell", "2", "220", second=2),
        ]
    )
    assert result.cost_status == "partial" and result.realized_pnl_quote is None


@pytest.mark.parametrize(
    "trades",
    [
        [fill("2", "sell", "1", "110", second=1), fill("1", "buy", "1", "100")],
        [fill("buy", "buy", "1", "100"), fill("sell", "sell", "1", "110")],
        [fill("2", "buy", "1", "100"), fill("1", "sell", "1", "110")],
    ],
)
def test_uncertain_or_reversed_execution_order_is_rejected(trades):
    with pytest.raises(ValueError):
        analyze(trades)


@pytest.mark.parametrize(
    "trade",
    [
        fill("1", "buy", "1", "100", second=61),
        fill("1", "buy", "1", "100", fee="1", asset="BTC"),
        fill("1", "sell", "1", "100", fee="101"),
    ],
)
def test_future_facts_and_impossible_fees_are_rejected(trade):
    with pytest.raises(ValueError):
        analyze([trade])


def test_frozen_copy_validation_rejects_forged_scope_and_extreme_money():
    valid = fill("1", "buy", "1", "100")
    for updates in (dict(account_ref="another"), dict(quantity=Decimal("1e129"))):
        with pytest.raises(ValueError):
            analyze([valid.model_copy(update=updates)])


def test_independent_decimal_context_and_residual_allocation_conserve_cost():
    trades = [
        fill("1", "buy", "3", "100"),
        *(fill(str(i + 2), "sell", "1", "40", second=i + 1) for i in range(3)),
    ]
    with localcontext() as context:
        context.prec, context.Emax, context.Emin = 2, 3, -3
        result = analyze(trades)
    assert result.realized_pnl_quote == Decimal("20")
    assert len({item.cost_quote for item in result.matches}) > 1
    assert result.inventory_quantity == 0
    with localcontext() as context:
        context.prec = 2048
        assert sum(item.cost_quote for item in result.matches) == Decimal("100")
        assert sum(item.realized_pnl_quote for item in result.matches) == Decimal("20")


def test_result_is_deterministic_and_source_facts_remain_unchanged():
    trades = [fill("1", "buy", "1", "100"), fill("2", "sell", "1", "110", second=1)]
    before = tuple(trade.model_dump_json() for trade in trades)
    first, second = analyze(trades), analyze(trades)
    assert first == second and len(first.source_sha256) == 64
    assert first.algorithm_version == "spot-fifo-v1"
    assert tuple(trade.model_dump_json() for trade in trades) == before
