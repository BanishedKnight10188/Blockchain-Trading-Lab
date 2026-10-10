"""Bounded deterministic Spot FIFO; no guessed transfers, fees or USD conversion."""

from collections import deque
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import reduce
from hashlib import sha256
from json import dumps

from agent_platform.domain.account import CostStatus, OrderSide, TradeBatch
from agent_platform.domain.common import exact_add, utc_datetime
from agent_platform.domain.review_facts import FifoLot, FifoMatch, FifoResult, InventoryCoverage

_CONTEXT = Context(prec=512, Emax=999999, Emin=-999999, rounding=ROUND_HALF_EVEN)


def _bounded(value):
    if value is not None and (
        len(value.as_tuple().digits) > 128 or abs(value.as_tuple().exponent) > 128
    ):
        raise ValueError("FIFO monetary inputs exceed the supported exact range")


def _subtract(left, right):
    return exact_add(left, right.copy_negate())


class FifoAnalyzer:
    def analyze(
        self, batch: TradeBatch, cutoff: datetime, coverage: InventoryCoverage | None = None
    ) -> FifoResult:
        checked = TradeBatch.model_validate_json(batch.model_dump_json())
        cutoff = utc_datetime(cutoff)
        proof = (
            InventoryCoverage.model_validate_json(coverage.model_dump_json()) if coverage else None
        )
        if (
            checked.symbol != "BTCUSDT"
            or checked.market_type != "spot"
            or len(checked.trades) > 4096
        ):
            raise ValueError("FIFO supports bounded Spot BTCUSDT history")
        previous = None
        for trade in checked.trades:
            if trade.executed_at > cutoff or len(trade.trade_id) > 128:
                raise ValueError("trade is outside the bounded evidence cutoff")
            if previous is not None and (
                previous.executed_at > trade.executed_at
                or (
                    previous.executed_at == trade.executed_at
                    and (
                        not previous.trade_id.isascii()
                        or not trade.trade_id.isascii()
                        or not previous.trade_id.isdecimal()
                        or not trade.trade_id.isdecimal()
                        or int(previous.trade_id) >= int(trade.trade_id)
                    )
                )
            ):
                raise ValueError("FIFO requires certain chronological execution order")
            for value in (trade.price, trade.quantity, trade.quote_quantity, trade.fee):
                _bounded(value)
            previous = trade
        if proof:
            if (
                proof.account_ref != checked.account_ref
                or proof.symbol != checked.symbol
                or proof.market_type != checked.market_type
            ):
                raise ValueError("inventory coverage belongs to another trading scope")
            for amount in (
                proof.opening_base_quantity,
                proof.ending_base_quantity,
                proof.nontrade_base_movements_abs,
            ):
                _bounded(amount)
            if proof.as_of != cutoff or any(
                trade.executed_at < proof.period_start for trade in checked.trades
            ):
                raise ValueError("coverage must include exactly the review period")
        source = dumps(
            dict(
                batch=checked.model_dump(mode="json"),
                coverage=proof.model_dump(mode="json") if proof else None,
                cutoff=cutoff.isoformat(),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        with localcontext(_CONTEXT):
            return self._calculate(checked, cutoff, proof, sha256(source.encode()).hexdigest())

    @staticmethod
    def _calculate(batch, cutoff, proof, digest):
        lots, matches = deque(), []
        reasons = set()
        unmatched = Decimal(0)
        if not batch.history_complete:
            reasons.add("incomplete_trade_history")
        if proof is None:
            reasons.add("missing_inventory_coverage")
        else:
            if not proof.movements_complete:
                reasons.add("incomplete_asset_movements")
            if proof.nontrade_base_movements_abs is None:
                reasons.add("unverified_base_asset_movements")
            elif proof.nontrade_base_movements_abs != 0:
                reasons.add("nontrade_base_asset_movements")
            if proof.opening_base_quantity:
                reasons.add("unknown_opening_cost")
                lots.append([None, proof.opening_base_quantity, None])
        if not batch.trades:
            reasons.add("no_trades")
        for trade in batch.trades:
            base_fee = trade.fee if trade.fee_asset == "BTC" else Decimal(0)
            quote_fee = trade.fee if trade.fee_asset == "USDT" else Decimal(0)
            quote = trade.quote_quantity
            if quote is None:
                reasons.add("missing_quote_quantity")
            unknown_fee = bool(trade.fee and trade.fee_asset not in ("BTC", "USDT"))
            if unknown_fee:
                reasons.add("unvalued_fee_asset")
            if trade.side == OrderSide.BUY:
                if base_fee >= trade.quantity:
                    raise ValueError("buy fee consumes all purchased inventory")
                lots.append(
                    [
                        trade.trade_id,
                        trade.quantity - base_fee,
                        quote + quote_fee if quote is not None and not unknown_fee else None,
                    ]
                )
                continue
            if quote is not None and quote_fee > quote:
                raise ValueError("sell quote fee exceeds actual proceeds")
            consumed = trade.quantity + base_fee
            proceeds = quote - quote_fee if quote is not None and not unknown_fee else None
            remaining_quantity, remaining_proceeds = consumed, proceeds
            while remaining_quantity and lots:
                buy_id, lot_quantity, lot_cost = lots[0]
                quantity = min(lot_quantity, remaining_quantity)
                cost = (
                    None
                    if lot_cost is None
                    else (
                        lot_cost if quantity == lot_quantity else lot_cost * quantity / lot_quantity
                    )
                )
                allocated = (
                    None
                    if remaining_proceeds is None
                    else (
                        remaining_proceeds
                        if quantity == remaining_quantity
                        else remaining_proceeds * quantity / remaining_quantity
                    )
                )
                matches.append(
                    FifoMatch(
                        buy_trade_id=buy_id,
                        sell_trade_id=trade.trade_id,
                        inventory_quantity=quantity,
                        cost_quote=cost,
                        proceeds_quote=allocated,
                    )
                )
                remaining_quantity = _subtract(remaining_quantity, quantity)
                if allocated is not None:
                    remaining_proceeds = _subtract(remaining_proceeds, allocated)
                if quantity == lot_quantity:
                    lots.popleft()
                else:
                    lots[0] = [
                        buy_id,
                        _subtract(lot_quantity, quantity),
                        None if lot_cost is None else _subtract(lot_cost, cost),
                    ]
            if remaining_quantity:
                matches.append(
                    FifoMatch(
                        buy_trade_id=None,
                        sell_trade_id=trade.trade_id,
                        inventory_quantity=remaining_quantity,
                        cost_quote=None,
                        proceeds_quote=remaining_proceeds,
                    )
                )
                unmatched = exact_add(unmatched, remaining_quantity)
                reasons.add("missing_inventory_basis")
        inventory = reduce(exact_add, (lot[1] for lot in lots), Decimal(0))
        if proof and inventory != proof.ending_base_quantity:
            reasons.add("inventory_reconciliation_gap")
        known_matches = sum(
            item.cost_quote is not None and item.proceeds_quote is not None for item in matches
        )
        status = (
            CostStatus.KNOWN
            if not reasons
            else (CostStatus.PARTIAL if known_matches else CostStatus.UNKNOWN)
        )
        pnl = None
        if status == CostStatus.KNOWN:
            matches = [
                item.model_copy(
                    update={"realized_pnl_quote": _subtract(item.proceeds_quote, item.cost_quote)}
                )
                for item in matches
            ]
            pnl = (
                reduce(exact_add, (item.realized_pnl_quote for item in matches), Decimal(0))
                if matches
                else None
            )
        return FifoResult(
            account_ref=batch.account_ref,
            data_cutoff=cutoff,
            source_sha256=digest,
            cost_status=status,
            reasons=tuple(sorted(reasons)),
            matches=tuple(matches),
            remaining_lots=tuple(
                FifoLot(buy_trade_id=identity, quantity=quantity, cost_quote=cost)
                for identity, quantity, cost in lots
            ),
            inventory_quantity=inventory,
            unmatched_sold_quantity=unmatched,
            realized_pnl_quote=pnl,
        )
