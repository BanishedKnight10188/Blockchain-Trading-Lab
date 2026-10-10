"""Frozen group and per-version evidence; later prose cannot replace source facts."""

from hashlib import sha256
from json import dumps
from typing import Self

from pydantic import Field, StrictBool, model_validator

from .account import TradeBatch
from .decisions import DecisionSnapshot
from .models import DomainModel, Identifier, LiveAccountRef, Revision, UtcDateTime
from .review_checks import TradeReviewCheck
from .review_facts import FifoResult
from .reviews import ReviewKind, ReviewRevision, TradeAttribution, TradeGroup


def review_identity(group_id: str, cutoff, kind: ReviewKind) -> str:
    identity = dumps([group_id, cutoff.isoformat(), str(kind)], separators=(",", ":"))
    return "review-version:" + sha256(identity.encode()).hexdigest()


def rule_review_explanation(cost_status, kind) -> str:
    return (
        "免费规则复盘；模型未参与。基于冻结成交，成本状态为"
        + str(cost_status)
        + "；成本或资金移动证据不完整时不计算完整盈亏。"
        + "原始归属按本次复盘版本保存，未确认成交保持未归属。"
        + (
            "本版为事后补充；数据截止时间与原始版本分别保存。"
            "后见行情仅引用截止前已提交的快照，无快照时不可评价后续走势。"
            if kind == ReviewKind.FOLLOWUP
            else ""
        )
    )


class ReviewContextEvidence(DomainModel):
    request_id: Identifier
    completion_event_id: Identifier
    observed_at: UtcDateTime
    snapshot: DecisionSnapshot

    @model_validator(mode="after")
    def recorded_after_capture(self) -> Self:
        if self.snapshot.captured_at > self.observed_at:
            raise ValueError("retrospective evidence must already have been observed")
        return self


class FrozenReviewGroup(DomainModel):
    group: TradeGroup
    batch: TradeBatch
    facts: FifoResult
    registered_at: UtcDateTime
    coverage: None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.group.trades != self.batch.trades
            or self.group.account_ref != self.batch.account_ref
            or self.group.symbol != "BTCUSDT"
            or self.group.market_type != "spot"
            or self.facts.account_ref != self.group.account_ref
            or self.group.cost_status != self.facts.cost_status
            or self.facts.data_cutoff > self.registered_at
            or self.batch.history_complete
            or len(self.group.trade_group_id) > 128
        ):
            raise ValueError("frozen group must preserve bounded actual source facts")
        return self

    @property
    def aggregate_id(self) -> str:
        return "review-group:" + sha256(self.group.trade_group_id.encode()).hexdigest()


class ReviewGroupPage(DomainModel):
    groups: tuple[FrozenReviewGroup, ...] = Field(max_length=50)
    next_sequence: int = Field(strict=True, ge=0, le=2**63 - 1)
    has_more: StrictBool


class ReviewProposal(DomainModel):
    trade_group_id: Identifier
    account_ref: LiveAccountRef
    kind: ReviewKind
    data_cutoff: UtcDateTime
    generated_at: UtcDateTime
    explanation: Identifier

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if (
            len(self.trade_group_id) > 128
            or len(self.explanation) > 4096
            or self.data_cutoff > self.generated_at
        ):
            raise ValueError("review proposal must have bounded content and a known cutoff")
        return self

    @property
    def aggregate_id(self) -> str:
        return review_identity(self.trade_group_id, self.data_cutoff, self.kind)


class ReviewRecord(DomainModel):
    review: ReviewRevision
    account_ref: LiveAccountRef
    facts: FifoResult
    attributions: tuple[TradeAttribution, ...] = Field(max_length=4096)
    attribution_revisions: tuple[Revision, ...] = Field(max_length=4096)
    attribution_event_ids: tuple[Identifier, ...] = Field(max_length=4096)
    trade_checks: tuple[TradeReviewCheck, ...] = Field(max_length=4096)
    retrospective_context: ReviewContextEvidence | None = None

    @model_validator(mode="after")
    def coherent_version(self) -> Self:
        if (
            self.review.review_id != self.aggregate_id
            or self.facts.account_ref != self.account_ref
            or self.review.cost_status != self.facts.cost_status
            or self.review.model_participated
            or self.review.realized_pnl_usd is not None
            or self.review.data_cutoff < self.facts.data_cutoff
            or len(self.attributions) != len(self.attribution_revisions)
            or len(self.attributions) != len(self.attribution_event_ids)
            or tuple(item.trade_id for item in self.trade_checks)
            != tuple(item.trade_id for item in self.attributions)
            or any(
                item.account_ref != self.account_ref
                or item.market_type != "spot"
                or item.symbol != "BTCUSDT"
                for item in self.attributions
            )
        ):
            raise ValueError("rule review must preserve frozen facts and attribution scope")
        context = self.retrospective_context
        if context is not None and (
            self.review.kind == ReviewKind.INITIAL
            or context.snapshot.account.account_ref != self.account_ref
            or context.snapshot.account.market_type != "spot"
            or context.snapshot.market.symbol != "BTCUSDT"
            or not self.facts.data_cutoff < context.snapshot.captured_at
            or context.observed_at > self.review.data_cutoff
        ):
            raise ValueError("later market evidence must be scoped and known before this cutoff")
        return self

    @property
    def aggregate_id(self) -> str:
        return review_identity(
            self.review.trade_group_id, self.review.data_cutoff, self.review.kind
        )
