"""Bounded internal frames and deliberately reduced browser projections."""

from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .account import AccountSyncStatus, Balance, CostStatus
from .advice_views import AdviceView
from .market import FeatureSnapshot, MarketSnapshot
from .models import DomainModel, Identifier, NonnegativeAmount, PositiveAmount, UtcDateTime
from .runtime_views import DecisionRuntimeView
from .sync import SyncReport

DataMode = Literal["disabled", "fake", "replay", "live_read_only"]
MarketSource = Literal["none", "fake", "replay", "binance_direct"]
AccountSource = Literal["none", "fake", "binance_direct"]


class OverviewFrame(DomainModel):
    event_id: Identifier = "initial"
    mode: DataMode
    market_source: MarketSource = "none"
    account_source: AccountSource = "none"
    captured_at: UtcDateTime
    market: MarketSnapshot | None = None
    features: FeatureSnapshot | None = None
    sync: SyncReport | None = None
    market_error: Literal["not_connected", "transport", "invalid_data"] | None = None
    account_error: Literal["not_connected", "persistence", "invalid_data", "transport"] | None = (
        None
    )

    @model_validator(mode="after")
    def coherent_frame(self) -> Self:
        if self.mode == "disabled" and (
            self.market_source != "none"
            or self.account_source != "none"
            or self.market is not None
            or self.sync is not None
            or self.features is not None
        ):
            raise ValueError("disabled frame cannot contain observations")
        if self.mode == "live_read_only" and (
            self.market_source not in ("none", "binance_direct")
            or self.account_source not in ("none", "binance_direct")
        ):
            raise ValueError("live frame cannot contain offline providers")
        if self.mode in ("fake", "replay") and (
            self.market_source == "binance_direct" or self.account_source == "binance_direct"
        ):
            raise ValueError("offline frame cannot contain live providers")
        for value in (self.market, self.features):
            if value is not None and (value.symbol != "BTCUSDT" or value.as_of > self.captured_at):
                raise ValueError("frame contains future or out-of-scope observations")
        if self.features is not None and (
            self.market is None or self.features.as_of != self.market.as_of
        ):
            raise ValueError("features must correspond to the market frame")
        if self.market is not None and self.market_source == "none":
            raise ValueError("market observations require an explicit source")
        if self.sync is not None and (
            self.account_source == "none" or self.sync.attempted_at > self.captured_at
        ):
            raise ValueError("account observations require a source and valid time")
        return self


class MarketOverview(DomainModel):
    source: MarketSource
    status: Literal["not_connected", "warming", "ready", "stale", "gap", "unavailable"]
    as_of: UtcDateTime | None = None
    quote_at: UtcDateTime | None = None
    book_at: UtcDateTime | None = None
    book_status: Literal["unavailable", "ready", "stale"] = "unavailable"
    price: PositiveAmount | None = None
    bid: PositiveAmount | None = None
    ask: PositiveAmount | None = None
    feature_status: Literal["unavailable", "warming", "ready", "stale"] = "unavailable"
    features: FeatureSnapshot | None = None
    error: Literal["not_connected", "transport", "invalid_data"] | None = None


class OrderOverview(DomainModel):
    side: Literal["buy", "sell"]
    status: Identifier
    quantity: PositiveAmount
    filled_quantity: NonnegativeAmount
    price: NonnegativeAmount
    updated_at: UtcDateTime


class AccountOverview(DomainModel):
    source: AccountSource
    status: AccountSyncStatus | Literal["not_connected"]
    as_of: UtcDateTime | None = None
    attempted_at: UtcDateTime | None = None
    next_attempt_at: UtcDateTime | None = None
    balances: tuple[Balance, ...] = ()
    quantity: NonnegativeAmount | None = None
    cost_status: CostStatus = CostStatus.UNKNOWN
    average_cost: PositiveAmount | None = None
    orders: tuple[OrderOverview, ...] = ()
    order_count: int | None = Field(default=None, strict=True, ge=0)
    orders_as_of: UtcDateTime | None = None
    orders_status: Literal["unavailable", "fresh", "stale"] = "unavailable"
    history_complete: StrictBool = False
    error: (
        Literal[
            "not_connected",
            "persistence",
            "invalid_data",
            "transport",
            "credentials",
            "authentication",
            "rate_limit",
        ]
        | None
    ) = None


class OverviewView(DomainModel):
    event_id: Identifier
    mode: DataMode
    generated_at: UtcDateTime
    captured_at: UtcDateTime
    market: MarketOverview
    account: AccountOverview
    advice_status: Literal[
        "unavailable", "pending", "published", "expired", "superseded", "accepted", "rejected"
    ] = "unavailable"
    advice_reason: Identifier | None = "decision_pipeline_not_connected"
    advice: AdviceView | None = None
    decision_runtime: DecisionRuntimeView | None = None
    jev_status: Literal["unspecified"] = "unspecified"
    paid_models_enabled: Literal[False] = False
