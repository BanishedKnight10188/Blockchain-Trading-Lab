"""User-confirmed decision attribution separate from exchange execution facts."""

from enum import StrEnum
from hashlib import sha256
from json import dumps
from typing import Literal, Self

from pydantic import StrictBool, model_validator

from .account import CostStatus, ObservedTrade
from .common import DecisionOrigin, MarketType
from .models import DomainModel, FiniteDecimal, Identifier, LiveAccountRef, Revision, UtcDateTime


class TradeAttribution(DomainModel):
    venue: Literal["binance"] = "binance"
    market_type: MarketType = MarketType.SPOT
    account_ref: LiveAccountRef
    symbol: Identifier
    trade_id: Identifier
    original_author: DecisionOrigin = DecisionOrigin.UNCLASSIFIED
    final_decision_maker: DecisionOrigin = DecisionOrigin.UNCLASSIFIED
    executor: Literal["human"] = "human"
    recommendation_id: Identifier | None = None
    user_confirmed: StrictBool = False

    @model_validator(mode="after")
    def no_implicit_attribution(self) -> Self:
        classified = (
            self.recommendation_id is not None
            or self.original_author != DecisionOrigin.UNCLASSIFIED
            or self.final_decision_maker != DecisionOrigin.UNCLASSIFIED
        )
        if classified and not self.user_confirmed:
            raise ValueError("attribution requires explicit user confirmation")
        return self

    @property
    def identity(self) -> tuple[str, ...]:
        return self.venue, self.market_type, self.account_ref, self.symbol, self.trade_id

    @property
    def aggregate_id(self) -> str:
        canonical = dumps(self.identity, ensure_ascii=True, separators=(",", ":"))
        return "trade-attribution:" + sha256(canonical.encode("utf-8")).hexdigest()


class AttributionReceipt(DomainModel):
    attribution: TradeAttribution
    revision: Revision
    event_id: Identifier
    recorded_at: UtcDateTime | None = None


class TradeGroup(DomainModel):
    trade_group_id: Identifier
    account_ref: LiveAccountRef
    symbol: Identifier
    market_type: MarketType = MarketType.SPOT
    trades: tuple[ObservedTrade, ...]
    cost_status: CostStatus

    @model_validator(mode="after")
    def coherent_facts(self) -> Self:
        if not self.trades:
            raise ValueError("trade group requires actual trades")
        if any(
            trade.account_ref != self.account_ref
            or trade.symbol != self.symbol
            or trade.market_type != self.market_type
            for trade in self.trades
        ):
            raise ValueError("trade group contains another scope")
        if len({trade.trade_id for trade in self.trades}) != len(self.trades):
            raise ValueError("trade group contains duplicate trades")
        times = tuple(trade.executed_at for trade in self.trades)
        if times != tuple(sorted(times)):
            raise ValueError("trade group facts must be chronological")
        return self


class ReviewKind(StrEnum):
    INITIAL = "initial"
    FOLLOWUP = "followup"
    MANUAL = "manual"


class ReviewRevision(DomainModel):
    review_id: Identifier
    trade_group_id: Identifier
    revision: Revision
    parent_review_id: Identifier | None = None
    kind: ReviewKind
    data_cutoff: UtcDateTime
    generated_at: UtcDateTime
    evidence_ids: tuple[Identifier, ...]
    explanation: Identifier
    cost_status: CostStatus
    realized_pnl_usd: FiniteDecimal | None = None
    model_participated: StrictBool = False

    @model_validator(mode="after")
    def honest_revision(self) -> Self:
        if (self.revision == 1) != (self.parent_review_id is None):
            raise ValueError("review revisions require a parent after the first version")
        if self.parent_review_id == self.review_id:
            raise ValueError("review cannot be its own parent")
        if self.kind == ReviewKind.INITIAL and self.revision != 1:
            raise ValueError("initial review must be the first version")
        if self.kind == ReviewKind.FOLLOWUP and self.revision == 1:
            raise ValueError("followup review must reference an earlier version")
        if self.data_cutoff > self.generated_at:
            raise ValueError("review cannot use information beyond its generation time")
        if self.cost_status != CostStatus.KNOWN and self.realized_pnl_usd is not None:
            raise ValueError("incomplete cost cannot claim realized profit or loss")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("review evidence identifiers must be unique")
        return self

    @property
    def is_retrospective(self) -> bool:
        return self.kind == ReviewKind.FOLLOWUP


class ReviewJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class ReviewJob(DomainModel):
    job_id: Identifier
    trade_group_id: Identifier
    kind: ReviewKind
    created_at: UtcDateTime
    scheduled_for: UtcDateTime
    status: ReviewJobStatus = ReviewJobStatus.PENDING
    review_id: Identifier | None = None
    failure_reason: Identifier | None = None

    @model_validator(mode="after")
    def coherent_job(self) -> Self:
        if self.scheduled_for < self.created_at:
            raise ValueError("review job cannot be scheduled before creation")
        if (self.status == ReviewJobStatus.DONE) != (self.review_id is not None):
            raise ValueError("only completed review jobs carry their result identifier")
        if self.failure_reason is not None and (
            self.status != ReviewJobStatus.FAILED or len(self.failure_reason) > 128
        ):
            raise ValueError("failure reason belongs to a bounded failed job")
        return self

    @property
    def identity(self) -> tuple:
        return self.trade_group_id, self.kind, self.scheduled_for
