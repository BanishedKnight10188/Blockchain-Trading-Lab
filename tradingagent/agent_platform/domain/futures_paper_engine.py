"""Pure USDT isolated fills, settlement and loss caps; no model or exchange calls."""

from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import wraps

from .common import utc_datetime
from .futures_paper import (
    FuturesPaperFunding,
    FuturesPaperOperation,
    FuturesPaperOrder,
    FuturesPaperQuote,
    FuturesPaperRules,
    FuturesPaperSettings,
    FuturesPaperState,
    FuturesPaperTransition,
    FuturesPaperValuation,
)

CASH = Decimal("1e-12")
COST = Decimal("1e-24")


def arithmetic(function):
    @wraps(function)
    def run(*args, **kwargs):
        with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
            return function(*args, **kwargs)

    return run


def checked(model, value):
    return model.model_validate_json(value.model_dump_json())


def ceil_cash(value):
    return value.quantize(CASH, rounding=ROUND_CEILING)


def floor_cash(value):
    return value.quantize(CASH, rounding=ROUND_FLOOR)


@arithmetic
def create_account(session_id, symbol, settings, rules, at):
    settings = checked(FuturesPaperSettings, settings)
    rules = checked(FuturesPaperRules, rules)
    return FuturesPaperState(
        account_ref="paper:futures:" + session_id,
        session_id=session_id,
        symbol=symbol,
        settings=settings,
        rules=rules,
        free_usdt=settings.initial_usdt,
        created_at=at,
        updated_at=at,
    )


def evidence(state, quote, at, *, book=False):
    at = utc_datetime(at)
    if quote.symbol != state.symbol or at < state.updated_at or quote.received_at > at:
        raise ValueError("simulation evidence identity or chronology conflicts")
    stamps = (quote.mark_at, quote.book_at) if book else (quote.mark_at,)
    if any(stamp > at or at - stamp > timedelta(seconds=5) for stamp in stamps):
        raise ValueError("simulation requires fresh nonfuture evidence")
    previous = state.last_quote
    if previous is not None:
        if (
            quote.source != previous.source
            or quote.received_at < previous.received_at
            or quote.mark_at < previous.mark_at
            or (quote.mark_at == previous.mark_at and quote.mark != previous.mark)
        ):
            raise ValueError("mark evidence is older than or conflicts with its watermark")
        if book and (
            quote.book_at < previous.book_at
            or (
                quote.book_at == previous.book_at
                and (quote.bid, quote.ask) != (previous.bid, previous.ask)
            )
        ):
            raise ValueError("book evidence is older than or conflicts with its watermark")


def position_pnl(state, mark):
    change = mark * state.quantity - state.entry_notional
    return floor_cash(change if state.side == "long" else -change) if state.quantity else Decimal(0)


def valuation(state, mark):
    pnl = position_pnl(state, mark)
    isolated = state.margin_usdt + pnl
    maintenance = ceil_cash(mark * state.quantity * state.rules.maintenance_margin_rate)
    return FuturesPaperValuation(
        equity_usdt=state.free_usdt + isolated,
        isolated_equity_usdt=isolated,
        unrealized_pnl_usdt=pnl,
        maintenance_margin_usdt=maintenance,
        requires_liquidation=bool(state.quantity and isolated <= maintenance),
    )


@arithmetic
def value(state, quote, at):
    state = checked(FuturesPaperState, state)
    quote = checked(FuturesPaperQuote, quote)
    evidence(state, quote, at)
    return valuation(state, quote.mark)


def result(state, at, changes, kind, **details):
    if kind in ("trade", "liquidation"):
        changes = changes | {"position_updated_at": at}
    updated = FuturesPaperState.model_validate(
        state.model_dump() | changes | {"revision": state.revision + 1, "updated_at": at}
    )
    operation = FuturesPaperOperation(
        kind=kind,
        before_revision=state.revision,
        after_revision=updated.revision,
        occurred_at=at,
        **details,
    )
    return FuturesPaperTransition(state=updated, operation=operation)


def liquidate(
    state,
    mark,
    at,
    *,
    original=None,
    funding_flow=Decimal(0),
    funding_shortfall=Decimal(0),
    quote=None,
):
    original = original or state
    mark_pnl = position_pnl(state, mark)
    absorbed = max(mark_pnl, -state.margin_usdt)
    remaining = max(Decimal(0), state.margin_usdt + mark_pnl)
    paid_debt = min(remaining, funding_shortfall)
    remaining -= paid_debt
    fee = min(remaining, ceil_cash(mark * state.quantity * state.rules.liquidation_fee_bps / 10000))
    deficit = max(Decimal(0), -state.margin_usdt - mark_pnl)
    return result(
        original,
        at,
        dict(
            status="liquidated",
            quantity=Decimal(0),
            side=None,
            entry_notional=Decimal(0),
            margin_usdt=Decimal(0),
            free_usdt=state.free_usdt + remaining - fee,
            realized_pnl_usdt=state.realized_pnl_usdt + absorbed,
            fees_usdt=state.fees_usdt + fee,
            funding_usdt=state.funding_usdt - paid_debt,
            last_funding_at=state.last_funding_at,
            shortfall_usdt=state.shortfall_usdt + deficit - paid_debt,
            last_quote=quote or state.last_quote,
        ),
        "liquidation",
        quantity=state.quantity,
        price=mark,
        side="sell" if state.side == "long" else "buy",
        realized_pnl_usdt=absorbed,
        fee_usdt=fee,
        funding_usdt=funding_flow - paid_debt,
        shortfall_usdt=deficit + funding_shortfall - paid_debt,
        reason="funding_exhausted" if funding_shortfall else "maintenance",
    )


@arithmetic
def execute(state, order, quote, at):
    state = checked(FuturesPaperState, state)
    order = checked(FuturesPaperOrder, order)
    quote = checked(FuturesPaperQuote, quote)
    at = utc_datetime(at)
    evidence(state, quote, at)
    current = valuation(state, quote.mark)
    if state.status == "liquidated":
        raise ValueError("liquidated simulation cannot resume trading")
    if current.requires_liquidation:
        return liquidate(state, quote.mark, at, quote=quote)
    if state.status != "running":
        raise ValueError("simulation trading must be explicitly running")
    evidence(state, quote, at, book=True)
    qty = order.quantity
    r = state.rules
    if qty % r.qty_step or qty > r.max_qty:
        raise ValueError("quantity does not satisfy contract step or maximum")
    reducing = order.action == "reduce"
    if reducing and (not state.quantity or qty > state.quantity):
        raise ValueError("reduction cannot create or reverse a position")
    direction = state.side if reducing else ("long" if order.action == "open_long" else "short")
    if not reducing and state.side is not None and state.side != direction:
        raise ValueError("position reversal requires a separate full close")
    buy = (direction == "short") if reducing else (direction == "long")
    raw = (
        (quote.ask * (1 + state.settings.slippage_bps / 10000))
        if buy
        else (quote.bid * (1 - state.settings.slippage_bps / 10000))
    )
    price = (raw / r.tick_size).to_integral_value(
        rounding=ROUND_CEILING if buy else ROUND_FLOOR
    ) * r.tick_size
    if price <= 0:
        raise ValueError("adverse simulation price is outside the contract tick")
    notional = qty * price
    fee = ceil_cash(notional * state.settings.fee_bps / 10000)
    if not reducing:
        leverage = order.target_leverage or state.settings.leverage
        ceiling = state.settings.max_leverage or state.settings.leverage
        if leverage > ceiling or r.maintenance_margin_rate * leverage >= 1:
            raise ValueError("leverage exceeds the confirmed or maintenance limit")
        if qty < r.min_qty or notional < r.min_notional:
            raise ValueError("increase is below contract minimum")
        total_qty = state.quantity + qty
        if (
            max(price, quote.mark) * total_qty > state.settings.max_position_notional
            or total_qty > r.max_qty
        ):
            raise ValueError("position exceeds hard notional or quantity limits")
        # Move only the initial-margin difference, preserving funding and any
        # existing collateral flow. This is atomic with the resulting fill.
        adjustment = ceil_cash(state.entry_notional / leverage) - ceil_cash(
            state.entry_notional / state.settings.leverage
        )
        margin = ceil_cash(notional / leverage) + adjustment
        if margin + fee > state.free_usdt:
            raise ValueError("insufficient free virtual USDT")
        if (
            state.settings.initial_usdt - (current.equity_usdt - fee)
            >= state.settings.max_run_loss_usdt
        ):
            raise ValueError("run loss limit prevents increased risk")
        changed = dict(
            free_usdt=state.free_usdt - margin - fee,
            margin_usdt=state.margin_usdt + margin,
            quantity=total_qty,
            side=direction,
            entry_notional=state.entry_notional + notional.quantize(COST),
            fees_usdt=state.fees_usdt + fee,
            settings=FuturesPaperSettings.model_validate(
                state.settings.model_dump() | {"leverage": leverage, "max_leverage": ceiling}
            )
            if order.target_leverage is not None
            else state.settings,
        )
        candidate = FuturesPaperState.model_validate(state.model_dump() | changed)
        after_value = valuation(candidate, quote.mark)
        if (
            state.settings.initial_usdt - after_value.equity_usdt
            >= state.settings.max_run_loss_usdt
        ):
            raise ValueError("increase including execution loss exceeds run loss limit")
        if after_value.requires_liquidation:
            raise ValueError("new position would violate maintenance margin")
        pnl = Decimal(0)
    else:
        full = qty == state.quantity
        entry = (
            state.entry_notional
            if full
            else (state.entry_notional * qty / state.quantity).quantize(COST)
        )
        released = (
            state.margin_usdt if full else floor_cash(state.margin_usdt * qty / state.quantity)
        )
        pnl = floor_cash((notional - entry) * (1 if state.side == "long" else -1))
        cash = released + pnl - fee
        if cash < 0:
            raise ValueError("reduction settlement exceeds its isolated collateral")
        changed = dict(
            free_usdt=state.free_usdt + cash,
            margin_usdt=state.margin_usdt - released,
            quantity=state.quantity - qty,
            side=None if full else state.side,
            entry_notional=state.entry_notional - entry,
            fees_usdt=state.fees_usdt + fee,
            realized_pnl_usdt=state.realized_pnl_usdt + pnl,
        )
    changed["last_quote"] = quote
    return result(
        state,
        at,
        changed,
        "trade",
        quantity=qty,
        price=price,
        side="buy" if buy else "sell",
        fee_usdt=fee,
        realized_pnl_usdt=pnl,
    )


@arithmetic
def settle_funding(state, funding, at):
    state = checked(FuturesPaperState, state)
    funding = checked(FuturesPaperFunding, funding)
    at = utc_datetime(at)
    if (
        funding.symbol != state.symbol
        or at < state.updated_at
        or funding.settled_at > at
        or funding.settled_at < state.created_at
        or (
            state.position_updated_at is not None
            and funding.settled_at <= state.position_updated_at
        )
        or (state.last_quote is not None and funding.source != state.last_quote.source)
        or (state.last_funding_at is not None and funding.settled_at <= state.last_funding_at)
    ):
        raise ValueError("funding identity or position chronology is inconsistent")
    nominal = (
        floor_cash(
            -funding.mark * state.quantity * funding.rate * (1 if state.side == "long" else -1)
        )
        if state.quantity
        else Decimal(0)
    )
    applied = max(nominal, -state.margin_usdt)
    shortage = applied - nominal
    changed = dict(
        margin_usdt=state.margin_usdt + applied,
        funding_usdt=state.funding_usdt + applied,
        shortfall_usdt=state.shortfall_usdt + shortage,
        last_funding_at=funding.settled_at,
    )
    if not state.quantity:
        return result(state, at, changed, "funding", funding_usdt=applied, shortfall_usdt=shortage)
    funded = FuturesPaperState.model_validate(state.model_dump() | changed | {"updated_at": at})
    if funded.margin_usdt == 0 or valuation(funded, funding.mark).requires_liquidation:
        return liquidate(
            funded,
            funding.mark,
            at,
            original=state,
            funding_flow=applied,
            funding_shortfall=shortage,
        )
    return result(state, at, changed, "funding", funding_usdt=applied, shortfall_usdt=shortage)
