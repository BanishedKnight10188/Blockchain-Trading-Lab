"""Strict engineering switches; credentials are not serializable configuration."""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from agent_platform.domain.market_archive import MarketRetentionPolicy
from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.domain.models import LiveAccountRef
from agent_platform.domain.operating_modes import OperatingSettings


class RuntimeConfigurationError(ValueError):
    pass


class FuturesCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    decision_seconds: int = Field(default=1, strict=True, ge=1, le=60)
    maintenance_seconds: int = Field(default=1, strict=True, ge=1, le=5)
    max_predictions: int = Field(default=3, strict=True, ge=1, le=3)
    prediction_ttl_seconds: int = Field(default=3, strict=True, ge=1, le=15)
    response_wait_seconds: int = Field(default=10, strict=True, ge=1, le=15)
    hourly_call_limit: int = Field(default=3600, strict=True, ge=1, le=3600)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    live_public: StrictBool = False
    futures_task_only: StrictBool = False
    futures_proxy: str | None = Field(default=None, strict=True)
    openrouter_proxy: str | None = Field(default=None, strict=True)
    live_account: StrictBool = False
    live_user_stream: StrictBool = False
    market_archive: StrictBool = True  # Applied only to explicitly enabled public market reads.
    market_retention: MarketRetentionPolicy = Field(default_factory=MarketRetentionPolicy)
    account_ref: LiveAccountRef = "binance-local"
    model_modules: ModelModulesConfig = Field(default_factory=ModelModulesConfig)
    operation: OperatingSettings = Field(default_factory=OperatingSettings)
    paper: StrictBool = False
    paper_mock: StrictBool = False
    paper_read_only: StrictBool = False
    paper_model_config: Path | None = None
    model_budget_database: Path | None = None
    model_budget_provider_managed: StrictBool = False
    futures_cadence: FuturesCadence = Field(default_factory=FuturesCadence)
    jev_workbench: StrictBool = False
    futures_multiscale: StrictBool = False
    background_model_config: Path | None = None
    event_agent: StrictBool = False
    event_agent_database: Path | None = None

    @model_validator(mode="after")
    def stream_requires_account(self):
        if self.model_budget_provider_managed and (not self.paper or self.paper_mock):
            raise ValueError("provider-managed fees require explicit real Paper configuration")
        if self.jev_workbench and (not self.paper or self.live_account or self.live_user_stream):
            raise ValueError(
                "JEV workbench requires isolated Paper without private account workers"
            )
        if self.paper_read_only and (not self.paper or self.paper_mock):
            raise ValueError("read-only model requires explicit real Paper configuration")
        if self.model_budget_database is not None and (not self.paper or self.paper_mock):
            raise ValueError("shared model budget requires explicit real Paper configuration")
        for name in ("futures_proxy", "openrouter_proxy"):
            address = getattr(self, name)
            if address is None:
                continue
            parsed = urlsplit(address)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in ("127.0.0.1", "localhost")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
            ):
                raise ValueError(f"{name} must be credential-free loopback HTTP")
        if self.paper:
            if self.paper_mock == (self.paper_model_config is not None):
                raise ValueError("paper requires exactly one explicit decision source")
            if self.paper_model_config is not None and not self.live_public:
                raise ValueError("real JEV paper requires explicit public market reads")
        elif self.paper_mock or self.paper_model_config is not None:
            raise ValueError("paper source requires paper opt-in")
        if self.live_user_stream and not self.live_account:
            raise ValueError("private read-only stream requires account polling opt-in")
        if (
            self.operation.mode == "auto"
            and self.operation.execution_environment == "testnet"
            and (self.live_public or self.live_account or self.live_user_stream)
        ):
            raise ValueError("testnet auto mode cannot use production read switches")
        return self
