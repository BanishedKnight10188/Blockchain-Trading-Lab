"""Account namespace shared by execution and evidence without import cycles."""

from typing import Literal, Self

from pydantic import ConfigDict, model_validator

from .models import DomainModel, Identifier
from .session_market import FuturesSymbol


class ExecutionScope(DomainModel):
    model_config = ConfigDict(revalidate_instances="always")
    environment: Literal["paper", "testnet"]
    account_ref: Identifier
    session_id: Identifier
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    symbol: FuturesSymbol

    @model_validator(mode="after")
    def namespace(self) -> Self:
        prefix = self.environment + ":futures:"
        if not self.account_ref.startswith(prefix) or self.account_ref == prefix:
            raise ValueError("execution environment and account namespace disagree")
        if self.environment == "paper" and self.account_ref != prefix + self.session_id:
            raise ValueError("paper account must belong to this session")
        return self
