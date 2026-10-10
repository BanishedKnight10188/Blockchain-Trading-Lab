"""A correction is an immutable user operation, separate from an exchange fill."""

from hashlib import sha256
from typing import Self

from pydantic import model_validator

from .models import DomainModel, Identifier, Revision, UtcDateTime
from .reviews import TradeAttribution


class AttributionChange(DomainModel):
    operation_id: Identifier
    attribution: TradeAttribution
    expected_revision: Revision
    explanation: Identifier
    recorded_at: UtcDateTime

    @model_validator(mode="after")
    def explicit_human_confirmation(self) -> Self:
        value = self.attribution
        if not value.user_confirmed or value.final_decision_maker != "human":
            raise ValueError("attribution requires explicit human confirmation")
        if value.market_type != "spot" or value.symbol != "BTCUSDT":
            raise ValueError("first release attribution requires BTCUSDT spot scope")
        if value.original_author != ("agent" if value.recommendation_id else "human"):
            raise ValueError("original attribution author must match its provenance")
        if (
            any(
                len(item) > 128
                for item in (
                    self.operation_id,
                    value.account_ref,
                    value.trade_id,
                    value.recommendation_id or "",
                )
            )
            or len(self.explanation) > 4096
        ):
            raise ValueError("attribution operation exceeds the bounded contract")
        return self

    @property
    def aggregate_id(self) -> str:
        return "attribution-change:" + sha256(self.operation_id.encode()).hexdigest()

    @property
    def updated_at(self):
        return self.recorded_at
