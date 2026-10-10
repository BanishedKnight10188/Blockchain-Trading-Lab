from typing import Literal

from .futures_values import Amount
from .models import DomainModel, Identifier, Revision
from .trade_intents import TradeIntent


class PositionProtection(DomainModel):
    protection_id: Identifier
    intent: TradeIntent
    revision: Revision = 1
    status: Literal["prepared", "ready", "closing", "closed", "degraded"] = "prepared"
    filled_quantity: Amount = "0"
    closing_command_id: Identifier | None = None
    reason: str | None = None


class ProtectionStatus(DomainModel):
    status: Literal["ready", "degraded", "not_configured"]
    protections: tuple[PositionProtection, ...] = ()
    reason: str | None = None


def protection_triggered(protection, lane, account, quote):
    stop = protection.intent.protective_stop_mark
    by_stop = stop is not None and (
        (account.side == "long" and quote.mark <= stop)
        or (account.side == "short" and quote.mark >= stop)
    )
    by_loss = (
        lane.limits is not None
        and account.equity_usdt is not None
        and lane.limits.initial_usdt - account.equity_usdt >= lane.limits.max_run_loss_usdt
    )
    return bool(account.quantity and (by_stop or by_loss))
