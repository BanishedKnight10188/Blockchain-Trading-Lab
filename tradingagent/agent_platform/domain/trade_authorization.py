"""User discipline independent of decision provenance."""

from .futures_values import Amount, Quantity
from .models import DomainModel
from .trading_runtime import TradingLimits, TradingPolicy


class FuturesRiskSettings(DomainModel):
    policy: TradingPolicy
    limits: TradingLimits
    qty_step: Quantity
    min_qty: Quantity
    max_qty: Quantity
    min_notional: Amount

    @classmethod
    def from_lane(cls, lane):
        if not lane.risk_configured:
            raise ValueError("risk_not_configured")
        return cls(
            **{name: getattr(lane, name) for name in cls.model_fields if name != "schema_version"}
        )
