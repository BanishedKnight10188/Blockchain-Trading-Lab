"""User intent and execution readiness are separate; selection grants no capability."""

from typing import Literal

from .models import DomainModel


class OperatingSettings(DomainModel):
    mode: Literal["advisory", "auto"] = "advisory"
    execution_environment: Literal["paper", "testnet"] = "testnet"

    def public_state(self, *, trader_enabled: bool) -> dict:
        # No executor is assembled in this phase. Later readiness must come from verified ports.
        state = "advisory_only"
        if self.mode == "auto":
            state = "execution_unconfigured" if trader_enabled else "trader_disabled"
        return {
            "mode": self.mode,
            "execution_environment": self.execution_environment,
            "decision_authority": "jev",
            "writes_enabled": False,
            "state": state,
        }
