"""Typed audit facts; arbitrary dictionaries and secrets are not payloads."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, model_validator

from .account import AccountSnapshot, TradeBatch, TradeCursor
from .agent_controls import AgentControlState
from .attribution import AttributionChange
from .costs import BudgetReservation, ModelUsage
from .decision_requests import DecisionCompletion, DecisionRequest
from .decisions import DecisionFeedback, DecisionSnapshot, Recommendation
from .feedback import FeedbackRecord
from .market import MarketEvent
from .models import DomainModel, Identifier, Revision, UtcDateTime
from .paper import PaperFill
from .paper_trading import PaperAccountState, PaperCycleState
from .paper_trials import PaperTrialState
from .reports import ReportState, ReportVerification, UserReportedTrade
from .review_records import FrozenReviewGroup, ReviewRecord
from .reviews import ReviewJob, ReviewRevision, TradeAttribution, TradeGroup
from .risk import RiskAssessment
from .sessions import AgentSession


class SessionJournalEvent(DomainModel):
    event_id: Identifier
    kind: Literal["created", "style_changed", "state_changed"]
    session: AgentSession
    occurred_at: UtcDateTime

    @model_validator(mode="after")
    def matches_state(self) -> Self:
        if self.occurred_at != self.session.updated_at:
            raise ValueError("event time must match session state")
        if self.kind == "created" and self.session.revision != 1:
            raise ValueError("creation must describe the first revision")
        return self


class StateType(StrEnum):
    PAPER_TRIAL = "paper_trial"
    PAPER_ACCOUNT = "paper_account"
    PAPER_CYCLE = "paper_cycle"
    AGENT_CONTROLS = "agent_controls"
    SESSION = "session"
    RECOMMENDATION = "recommendation"
    ATTRIBUTION = "attribution"
    REVIEW = "review"
    REVIEW_JOB = "review_job"
    FEEDBACK = "feedback"
    ATTRIBUTION_CHANGE = "attribution_change"
    USER_REPORT = "user_report"
    REPORT_VERIFICATION = "report_verification"
    REVIEW_GROUP = "review_group"
    REVIEW_RECORD = "review_record"


type OwnedState = (
    AgentControlState
    | PaperAccountState
    | PaperCycleState
    | PaperTrialState
    | AgentSession
    | Recommendation
    | TradeAttribution
    | ReviewRevision
    | ReviewJob
    | FeedbackRecord
    | AttributionChange
    | ReportState
    | ReportVerification
    | FrozenReviewGroup
    | ReviewRecord
)

_STATE_TYPES = {
    StateType.PAPER_TRIAL: PaperTrialState,
    StateType.PAPER_ACCOUNT: PaperAccountState,
    StateType.PAPER_CYCLE: PaperCycleState,
    StateType.AGENT_CONTROLS: AgentControlState,
    StateType.SESSION: AgentSession,
    StateType.RECOMMENDATION: Recommendation,
    StateType.ATTRIBUTION: TradeAttribution,
    StateType.REVIEW: ReviewRevision,
    StateType.REVIEW_JOB: ReviewJob,
    StateType.FEEDBACK: FeedbackRecord,
    StateType.ATTRIBUTION_CHANGE: AttributionChange,
    StateType.USER_REPORT: ReportState,
    StateType.REPORT_VERIFICATION: ReportVerification,
    StateType.REVIEW_GROUP: FrozenReviewGroup,
    StateType.REVIEW_RECORD: ReviewRecord,
}

_STATE_IDENTITIES = {
    StateType.PAPER_TRIAL: "aggregate_id",
    StateType.PAPER_ACCOUNT: "account_ref",
    StateType.PAPER_CYCLE: "aggregate_id",
    StateType.AGENT_CONTROLS: "aggregate_id",
    StateType.SESSION: "session_id",
    StateType.RECOMMENDATION: "recommendation_id",
    StateType.ATTRIBUTION: "aggregate_id",
    StateType.REVIEW: "trade_group_id",
    StateType.REVIEW_JOB: "job_id",
    StateType.FEEDBACK: "aggregate_id",
    StateType.ATTRIBUTION_CHANGE: "aggregate_id",
    StateType.USER_REPORT: "aggregate_id",
    StateType.REPORT_VERIFICATION: "aggregate_id",
    StateType.REVIEW_GROUP: "aggregate_id",
    StateType.REVIEW_RECORD: "aggregate_id",
}


class StateRecord(DomainModel):
    key: Identifier
    revision: Revision
    state_type: StateType
    state: OwnedState
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_tag(self) -> Self:
        if not isinstance(self.state, _STATE_TYPES[self.state_type]):
            raise ValueError("state tag does not match its owned type")
        internal_revision = getattr(self.state, "revision", self.revision)
        if self.revision != internal_revision:
            raise ValueError("state envelope must match the internal revision")
        if self.key != getattr(self.state, _STATE_IDENTITIES[self.state_type]):
            raise ValueError("state key must match the owned identity")
        timestamp = getattr(self.state, "updated_at", getattr(self.state, "generated_at", None))
        if timestamp is not None and self.updated_at != timestamp:
            raise ValueError("state envelope time must match its owned state")
        if hasattr(self.state, "created_at") and self.updated_at < self.state.created_at:
            raise ValueError("state envelope cannot predate its owned state")
        return self


class EventKind(StrEnum):
    STATE_CHANGED = "state_changed"
    SESSION_CHANGED = "session_changed"
    MARKET_OBSERVED = "market_observed"
    ACCOUNT_OBSERVED = "account_observed"
    TRADES_IMPORTED = "trades_imported"
    DECISION_SNAPSHOT_RECORDED = "decision_snapshot_recorded"
    DECISION_CLAIMED = "decision_claimed"
    DECISION_COMPLETED = "decision_completed"
    TRADE_GROUP_RECORDED = "trade_group_recorded"
    REVIEW_GROUP_FROZEN = "review_group_frozen"
    RECOMMENDATION_RECORDED = "recommendation_recorded"
    FEEDBACK_RECORDED = "feedback_recorded"
    USER_TRADE_REPORTED = "user_trade_reported"
    USER_TRADE_VERIFIED = "user_trade_verified"
    ATTRIBUTION_RECORDED = "attribution_recorded"
    REVIEW_RECORDED = "review_recorded"
    REVIEW_JOB_RECORDED = "review_job_recorded"
    RISK_ASSESSED = "risk_assessed"
    BUDGET_RESERVED = "budget_reserved"
    MODEL_USAGE_RECORDED = "model_usage_recorded"
    PAPER_FILLED = "paper_filled"


type JournalPayload = (
    StateRecord
    | AgentSession
    | MarketEvent
    | AccountSnapshot
    | TradeBatch
    | Recommendation
    | DecisionFeedback
    | TradeAttribution
    | ReviewRevision
    | ReviewJob
    | RiskAssessment
    | BudgetReservation
    | ModelUsage
    | PaperFill
    | DecisionSnapshot
    | TradeGroup
    | DecisionRequest
    | DecisionCompletion
    | UserReportedTrade
    | ReportVerification
    | FrozenReviewGroup
)

_EVENT_TYPES = {
    EventKind.STATE_CHANGED: (StateRecord, "key"),
    EventKind.SESSION_CHANGED: (AgentSession, "session_id"),
    EventKind.MARKET_OBSERVED: (MarketEvent, "symbol"),
    EventKind.ACCOUNT_OBSERVED: (AccountSnapshot, "account_ref"),
    EventKind.TRADES_IMPORTED: (TradeBatch, "account_ref"),
    EventKind.DECISION_SNAPSHOT_RECORDED: (DecisionSnapshot, "snapshot_id"),
    EventKind.DECISION_CLAIMED: (DecisionRequest, "request_id"),
    EventKind.DECISION_COMPLETED: (DecisionCompletion, "request_id"),
    EventKind.TRADE_GROUP_RECORDED: (TradeGroup, "trade_group_id"),
    EventKind.REVIEW_GROUP_FROZEN: (FrozenReviewGroup, "aggregate_id"),
    EventKind.RECOMMENDATION_RECORDED: (Recommendation, "recommendation_id"),
    EventKind.FEEDBACK_RECORDED: (DecisionFeedback, "feedback_id"),
    EventKind.USER_TRADE_REPORTED: (UserReportedTrade, "report_id"),
    EventKind.USER_TRADE_VERIFIED: (ReportVerification, "operation_id"),
    EventKind.ATTRIBUTION_RECORDED: (TradeAttribution, "aggregate_id"),
    EventKind.REVIEW_RECORDED: (ReviewRevision, "trade_group_id"),
    EventKind.REVIEW_JOB_RECORDED: (ReviewJob, "job_id"),
    EventKind.RISK_ASSESSED: (RiskAssessment, "snapshot_id"),
    EventKind.BUDGET_RESERVED: (BudgetReservation, "reservation_id"),
    EventKind.MODEL_USAGE_RECORDED: (ModelUsage, "request_id"),
    EventKind.PAPER_FILLED: (PaperFill, "intent_id"),
}


class JournalEvent(DomainModel):
    event_id: Identifier
    aggregate_id: Identifier
    kind: EventKind
    payload: JournalPayload
    occurred_at: UtcDateTime

    @model_validator(mode="after")
    def coherent_payload(self) -> Self:
        payload_type, identity_field = _EVENT_TYPES[self.kind]
        if not isinstance(self.payload, payload_type):
            raise ValueError("journal kind does not match payload")
        if self.aggregate_id != getattr(self.payload, identity_field):
            raise ValueError("journal aggregate does not match payload identity")
        return self


class AppendReceipt(DomainModel):
    event_id: Identifier
    sequence: int = Field(strict=True, ge=1)
    appended: StrictBool


class EventRecord(DomainModel):
    sequence: int = Field(strict=True, ge=1)
    event: JournalEvent


class EventPage(DomainModel):
    records: tuple[EventRecord, ...] = ()
    next_sequence: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def forward_sequence(self) -> Self:
        sequences = tuple(item.sequence for item in self.records)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("event page sequences must be ordered and unique")
        if sequences and self.next_sequence != sequences[-1]:
            raise ValueError("event cursor must match the last delivered sequence")
        return self


class ImportResult(DomainModel):
    account_ref: Identifier
    symbol: Identifier
    imported_count: int = Field(strict=True, ge=0)
    duplicate_count: int = Field(strict=True, ge=0)
    next_cursor: TradeCursor
    account_revision: int = Field(strict=True, ge=0)
    account: AccountSnapshot | None = None
    receipt: AppendReceipt


class Notice(DomainModel):
    notice_id: Identifier
    severity: Literal["info", "warning", "error"]
    message: Identifier
    occurred_at: UtcDateTime
    session_id: Identifier | None = None
