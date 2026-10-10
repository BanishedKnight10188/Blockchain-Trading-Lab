"""Hard discipline never reads style weights or depends on a model invocation."""

from datetime import timedelta
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from zoneinfo import ZoneInfo

from agent_platform.domain.account import AccountSyncStatus
from agent_platform.domain.decisions import AdviceAction, AdvisoryAssessment, DecisionSnapshot
from agent_platform.domain.risk import RiskAssessment, RiskContext
from agent_platform.ports.clock import ClockPort


def _bounded(value: Decimal | None) -> bool:
    return value is None or (
        len(value.as_tuple().digits) <= 128 and -128 <= value.as_tuple().exponent <= 128
    )


def _arithmetic() -> Context:
    return Context(
        prec=512,
        Emin=-1024,
        Emax=1024,
        rounding=ROUND_HALF_EVEN,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


class RiskService:
    def __init__(self, clock: ClockPort):
        self.clock = clock

    def evaluate(
        self,
        snapshot: DecisionSnapshot,
        proposed: AdvisoryAssessment,
        *,
        context: RiskContext | None = None,
    ) -> RiskAssessment:
        now, limits = self.clock.utcnow(), snapshot.limits
        context = context or RiskContext()
        block, missing = [], []
        amounts = [
            proposed.quantity,
            snapshot.position.quantity,
            limits.max_buy_quantity,
            limits.max_sell_quantity,
            limits.max_position_quantity,
            limits.max_daily_loss_usd,
            context.daily_loss_usd,
            context.cost_buffer_rate,
        ]
        amounts.extend(
            value
            for balance in snapshot.account.balances
            for value in (balance.free, balance.locked)
        )
        instrument, book = context.instrument, snapshot.market.book
        if book is not None:
            amounts.extend((book.bid, book.ask))
        if instrument is not None:
            amounts.extend(
                (
                    instrument.quantity_step,
                    instrument.price_tick,
                    instrument.min_quantity,
                    instrument.max_quantity,
                    instrument.min_notional,
                )
            )
        if not all(_bounded(value) for value in amounts):
            return RiskAssessment(
                snapshot_id=snapshot.snapshot_id,
                outcome="unavailable",
                reasons=("numeric_bounds",),
                evaluated_at=now,
            )
        if snapshot.captured_at > now:
            missing.append("snapshot_future")
        if now >= snapshot.trigger.expires_at:
            missing.append("trigger_expired")
        if snapshot.market.status != "ready":
            missing.append("market_not_ready")
        quote = snapshot.market.latest_quote_at
        if quote is None or not timedelta(0) <= now - quote <= timedelta(seconds=5):
            missing.append("market_quote_stale")
        if not snapshot.features.warmup_ready:
            missing.append("features_not_ready")
        if not timedelta(0) <= now - snapshot.features.as_of <= timedelta(seconds=60):
            missing.append("features_stale")
        confirmed = (
            snapshot.account.status == AccountSyncStatus.FRESH
            and snapshot.account.account_revision > 0
        )
        if not confirmed or not timedelta(0) <= now - snapshot.account.as_of <= timedelta(
            seconds=60
        ):
            missing.append("account_unavailable")
            confirmed = False
        if snapshot.market.symbol != "BTCUSDT" or snapshot.account.market_type != "spot":
            missing.append("unsupported_market_scope")
        if not set(proposed.evidence_ids) <= set(snapshot.evidence_ids):
            missing.append("advice_evidence_unknown")
        if proposed.action == AdviceAction.UNAVAILABLE:
            missing.append("proposal_unavailable")
        balances = {balance.asset: balance for balance in snapshot.account.balances}
        free_btc = balances["BTC"].free if "BTC" in balances else Decimal(0)
        free_usdt = balances["USDT"].free if "USDT" in balances else Decimal(0)
        if confirmed:
            total_btc = balances["BTC"].total if "BTC" in balances else Decimal(0)
            if snapshot.position.quantity != total_btc:
                missing.append("position_balance_mismatch")
                confirmed = False
            elif (proposed.action == AdviceAction.BUY and free_usdt <= 0) or (
                proposed.action == AdviceAction.SELL and free_btc <= 0
            ):
                block.append("insufficient_free_balance")
        if proposed.action == AdviceAction.BUY:
            if (
                limits.max_position_quantity is not None
                and snapshot.position.quantity >= limits.max_position_quantity
            ):
                block.append("position_limit_exceeded")
            if limits.max_daily_loss_usd is not None:
                sampled = context.daily_loss_as_of
                complete = (
                    context.daily_loss_complete
                    and sampled is not None
                    and timedelta(0) <= now - sampled <= timedelta(seconds=60)
                )
                if (
                    not complete
                    or sampled.astimezone(ZoneInfo("Asia/Shanghai")).date()
                    != now.astimezone(ZoneInfo("Asia/Shanghai")).date()
                ):
                    missing.append("daily_loss_unavailable")
                elif context.daily_loss_usd >= limits.max_daily_loss_usd:
                    block.append("daily_loss_limit_reached")
        quantity = proposed.quantity
        if quantity is not None:
            cap = (
                limits.max_buy_quantity
                if proposed.action == AdviceAction.BUY
                else limits.max_sell_quantity
            )
            if cap is None:
                block.append("quantity_limit_unconfigured")
            elif quantity > cap:
                block.append("quantity_limit_exceeded")
            if proposed.action == AdviceAction.BUY:
                if limits.max_position_quantity is None:
                    block.append("position_limit_unconfigured")
                else:
                    with localcontext(_arithmetic()):
                        if snapshot.position.quantity + quantity > limits.max_position_quantity:
                            block.append("position_limit_exceeded")
            if instrument is None:
                missing.append("instrument_unavailable")
            elif (
                instrument.market_type != "spot"
                or instrument.symbol != "BTCUSDT"
                or instrument.base_asset != "BTC"
                or instrument.quote_asset != "USDT"
            ):
                missing.append("instrument_scope_mismatch")
                instrument = None
            if context.cost_buffer_rate is None:
                missing.append("cost_buffer_unconfigured")
            if (
                book is None
                or snapshot.market.book_as_of is None
                or not timedelta(0) <= now - snapshot.market.book_as_of <= timedelta(seconds=5)
            ):
                missing.append("book_unavailable")
                book = None
            with localcontext(_arithmetic()):
                if instrument is not None:
                    if quantity < instrument.min_quantity:
                        block.append("quantity_below_minimum")
                    if instrument.max_quantity is not None and quantity > instrument.max_quantity:
                        block.append("exchange_quantity_exceeded")
                    if quantity % instrument.quantity_step:
                        block.append("quantity_step_mismatch")
                if book is not None:
                    price = book.ask if proposed.action == AdviceAction.BUY else book.bid
                    if instrument is not None and quantity * price < instrument.min_notional:
                        block.append("notional_below_minimum")
                    if confirmed and context.cost_buffer_rate is not None:
                        multiplier = Decimal(1) + context.cost_buffer_rate
                        required = (
                            quantity * price * multiplier
                            if proposed.action == AdviceAction.BUY
                            else quantity * multiplier
                        )
                        available = free_usdt if proposed.action == AdviceAction.BUY else free_btc
                        if required > available:
                            block.append("insufficient_free_balance")
        reasons = tuple(dict.fromkeys((*block, *missing)))
        return RiskAssessment(
            snapshot_id=snapshot.snapshot_id,
            outcome="block" if block else "unavailable" if missing else "allow",
            reasons=reasons,
            evaluated_at=now,
        )
