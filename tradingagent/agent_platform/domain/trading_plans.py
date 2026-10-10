"""Precomputed joint parameter choices; models never do order arithmetic."""

from decimal import ROUND_FLOOR, Context, Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator

from .futures_values import Amount, Price
from .models import DomainModel, Identifier


class FuturesTradePlan(DomainModel):
    candidate_id: Identifier
    intent: Literal["wait", "open_long", "open_short", "add_long", "add_short", "reduce", "close"]
    action: Literal["wait", "open_long", "open_short", "reduce"]
    sizing_basis: Literal["none", "equity_margin", "current_quantity"]
    percent: int = Field(strict=True, ge=0, le=100)
    quantity: Amount
    leverage: int | None = Field(default=None, strict=True, ge=1, le=20)
    reference_price: Price
    reference_quantity: Amount
    reference_equity: Amount | None = None

    @model_validator(mode="after")
    def coherent_plan(self):
        if self.intent == "wait":
            if (
                self.action != "wait"
                or self.quantity
                or self.percent
                or self.leverage is not None
                or self.sizing_basis != "none"
            ):
                raise ValueError("wait cannot alter exposure or leverage")
        elif self.quantity <= 0 or self.percent <= 0 or self.leverage is None:
            raise ValueError("a trade needs a positive sized plan and leverage")
        else:
            opening = self.intent.startswith("open_")
            expected = (
                "open_long"
                if self.intent.endswith("long")
                else "open_short"
                if self.intent.endswith("short")
                else "reduce"
            )
            if self.action != expected or self.sizing_basis != (
                "equity_margin" if opening else "current_quantity"
            ):
                raise ValueError("plan intent and sizing basis disagree")
            if opening and (self.reference_quantity or not self.reference_equity):
                raise ValueError("entry requires flat positive-equity account")
            if not opening and not self.reference_quantity:
                raise ValueError("position change requires a held quantity")
            if self.action == "reduce" and self.quantity > self.reference_quantity:
                raise ValueError("reduction cannot exceed the referenced quantity")
            if self.intent == "close" and (
                self.percent != 100 or self.quantity != self.reference_quantity
            ):
                raise ValueError("close must specify the entire referenced quantity")
        return self


def build_plans(run, account, quote) -> tuple[FuturesTradePlan, ...]:
    """Return immutable sizes at the captured account; execution rechecks all risk."""
    with localcontext(Context(prec=80)):
        equity = account.equity_usdt
        reference = dict(
            reference_price=quote.mark,
            reference_quantity=account.quantity,
            reference_equity=equity if equity is not None and equity >= 0 else None,
        )
        plans = [
            FuturesTradePlan(
                candidate_id="WAIT",
                intent="wait",
                action="wait",
                sizing_basis="none",
                percent=0,
                quantity=0,
                **reference,
            )
        ]
        if account.leverage is None:
            return tuple(plans)
        ceiling = run.limits.max_leverage or run.limits.leverage
        allowed = tuple(value for value in run.policy.leverage_choices if value <= ceiling)
        side = account.side
        price = max(quote.ask * (1 + run.limits.slippage_bps / 10000), quote.mark)

        def stepped(q):
            return (q / run.qty_step).to_integral_value(rounding=ROUND_FLOOR) * run.qty_step

        def add(intent, percent, leverage, quantity, basis):
            q = stepped(quantity)
            if (
                q < run.min_qty
                or q > run.max_qty
                or account.quantity + q > run.max_qty
                or q * quote.bid < run.min_notional
                or price * (account.quantity + q) > run.limits.max_position_notional
            ):
                return
            fee = q * price * run.limits.fee_bps / 10000
            adjustment = account.entry_notional * (
                Decimal(1) / leverage - Decimal(1) / account.leverage
            )
            if (
                equity is None
                or equity <= 0
                or adjustment + q * price / leverage + fee > account.free_usdt
                or run.limits.initial_usdt - equity + fee >= run.limits.max_run_loss_usdt
            ):
                return
            key = (
                f"{intent.upper()}_{'M' if basis == 'equity_margin' else 'Q'}{percent}_L{leverage}"
            )
            plans.append(
                FuturesTradePlan(
                    candidate_id=key,
                    intent=intent,
                    action="open_long" if intent.endswith("long") else "open_short",
                    sizing_basis=basis,
                    percent=percent,
                    quantity=q,
                    leverage=leverage,
                    **reference,
                )
            )

        if not account.quantity and equity is not None and equity > 0:
            for percent in run.policy.entry_margin_percents:
                for leverage in allowed:
                    q = equity * percent / 100 * leverage / price
                    for intent in ("open_long", "open_short"):
                        add(intent, percent, leverage, q, "equity_margin")
        elif account.quantity:
            for percent in run.policy.position_change_percents:
                for leverage in allowed:
                    add(
                        "add_" + side,
                        percent,
                        leverage,
                        account.quantity * percent / 100,
                        "current_quantity",
                    )
                if percent < 100:
                    q = stepped(account.quantity * percent / 100)
                    if q > 0:
                        plans.append(
                            FuturesTradePlan(
                                candidate_id=f"REDUCE_Q{percent}",
                                intent="reduce",
                                action="reduce",
                                sizing_basis="current_quantity",
                                percent=percent,
                                quantity=q,
                                leverage=account.leverage,
                                **reference,
                            )
                        )
            plans.append(
                FuturesTradePlan(
                    candidate_id="CLOSE",
                    intent="close",
                    action="reduce",
                    sizing_basis="current_quantity",
                    percent=100,
                    quantity=account.quantity,
                    leverage=account.leverage,
                    **reference,
                )
            )
        if len(plans) > 255:
            raise ValueError("too many joint decision plans")
        return tuple(plans)
