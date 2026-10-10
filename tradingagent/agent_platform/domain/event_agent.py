"""Paper-only event lane, finite context, turns and durable run facts."""

from datetime import timedelta
from typing import Literal, Self

from pydantic import Field, JsonValue, StrictBool, model_validator

from .agent_events import WatchEvent
from .agent_tools import ToolCall, ToolResult, ToolSpec, bounded_json
from .costs import ModelUsage, RouteDecision
from .model_calls import ProviderMetadata
from .models import (
    DomainModel,
    Identifier,
    NonnegativeAmount,
    PositiveAmount,
    Revision,
    UtcDateTime,
)
from .routing import bounded_money
from .trading_execution import ExecutionScope, TradingAccountSnapshot
from .trading_runtime import TradingLimits, TradingPolicy
from .watches import WatchDefinition, WatchEvaluation, WatchFeatures, fact_hash


class AgentBudgetGrant(DomainModel):
    grant_id: Identifier
    lane_id: Identifier
    model_id: Identifier
    price_version: Identifier
    lane_total_usd: NonnegativeAmount
    parent_total_usd: NonnegativeAmount
    max_single_cost_usd: NonnegativeAmount
    hourly_call_limit: int = Field(strict=True, ge=0, le=60)
    valid_from: UtcDateTime
    expires_at: UtcDateTime

    @model_validator(mode="after")
    def expires_after_grant(self) -> Self:
        if self.expires_at <= self.valid_from:
            raise ValueError("budget grant requires a positive lifetime")
        if not all(
            bounded_money(x)
            for x in (self.lane_total_usd, self.parent_total_usd, self.max_single_cost_usd)
        ):
            raise ValueError("budget grant arithmetic bounds")
        if self.max_single_cost_usd > min(self.lane_total_usd, self.parent_total_usd):
            raise ValueError("single request allowance exceeds total grant")
        return self


class EventAgentLane(DomainModel):
    lane_id: Identifier
    session_id: Identifier
    scope: ExecutionScope
    symbol: str = "BTCUSDT"
    revision: Revision = 1
    style_revision: Revision = 1
    agent_revision: Revision = 1
    policy_revision: Revision = 1
    enabled: StrictBool = False
    policy: TradingPolicy | None = None
    limits: TradingLimits | None = None
    qty_step: PositiveAmount | None = None
    min_qty: PositiveAmount | None = None
    max_qty: PositiveAmount | None = None
    min_notional: PositiveAmount | None = None

    @model_validator(mode="after")
    def paper_scope(self) -> Self:
        if (
            self.scope.environment != "paper"
            or self.scope.session_id != self.session_id
            or self.scope.symbol != self.symbol
        ):
            raise ValueError("event lane must own its Paper session and symbol")
        risk = (
            self.policy,
            self.limits,
            self.qty_step,
            self.min_qty,
            self.max_qty,
            self.min_notional,
        )
        if any(x is not None for x in risk) and not all(x is not None for x in risk):
            raise ValueError("risk configuration must be complete or absent")
        if self.max_qty is not None and max(self.min_qty, self.qty_step) > self.max_qty:
            raise ValueError("event lane has inconsistent quantity filters")
        return self

    @property
    def risk_configured(self) -> bool:
        return self.limits is not None


class AgentTriggerContext(DomainModel):
    event_id: Identifier
    definition: WatchDefinition
    evaluation: WatchEvaluation


class AgentMarketContext(DomainModel):
    captured_at: UtcDateTime
    candle: dict[str, JsonValue]
    features: WatchFeatures
    quality: Identifier
    input_hash: Identifier


class AgentContext(DomainModel):
    lane_id: Identifier
    scope: ExecutionScope
    captured_at: UtcDateTime
    policy: TradingPolicy | None = None
    limits: TradingLimits | None = None
    lane_revision: Revision = 1
    policy_revision: Revision = 1
    trigger: AgentTriggerContext | None = None
    latest_market: AgentMarketContext | None = None
    account: TradingAccountSnapshot | None = None
    unavailable: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bounded_scope(self) -> Self:
        if self.account is not None and self.account.scope != self.scope:
            raise ValueError("context account crossed lane scope")
        bounded_json(self.model_dump(mode="json"), 32768)
        return self

    @property
    def context_hash(self):
        return fact_hash(self)


class AgentMessage(DomainModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=32768)
    tool_calls: tuple[ToolCall, ...] = Field(default=(), max_length=8)
    tool_call_id: Identifier | None = None
    reasoning_details: tuple[JsonValue, ...] = ()

    @model_validator(mode="after")
    def role_channel(self) -> Self:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only an assistant message may request tools")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("only a tool result carries its call identity")
        if self.role in ("system", "user", "tool") and self.content is None:
            raise ValueError("message content is missing")
        if self.reasoning_details and self.role != "assistant":
            raise ValueError("opaque provider metadata belongs to the assistant")
        return self


class AgentTurnRequest(DomainModel):
    request_id: Identifier
    run_id: Identifier
    lane_id: Identifier
    captured_at: UtcDateTime
    deadline: UtcDateTime
    route: RouteDecision
    max_output_tokens: int = Field(default=2048, strict=True, ge=1, le=2048)
    context_hash: str = ""
    messages: tuple[AgentMessage, ...] = Field(min_length=1, max_length=24)
    tools: tuple[ToolSpec, ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def bounded_and_linked(self) -> Self:
        if not timedelta(0) < self.deadline - self.captured_at <= timedelta(seconds=30):
            raise ValueError("agent request deadline must be within 30 seconds")
        if len({t.name for t in self.tools}) != len(self.tools):
            raise ValueError("tool specifications must be unique")
        bounded_json(
            {
                "messages": [m.model_dump(mode="json") for m in self.messages],
                "tools": [t.model_dump(mode="json") for t in self.tools],
            },
            32768,
        )
        pending, seen = set(), set()
        for message in self.messages:
            if message.role != "tool" and pending:
                raise ValueError("all tool requests need results before another message")
            for call in message.tool_calls:
                if call.tool_call_id in seen:
                    raise ValueError("tool call identity was reused")
                pending.add(call.tool_call_id)
                seen.add(call.tool_call_id)
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("orphan tool result")
                pending.remove(message.tool_call_id)
        if pending:
            raise ValueError("request contains unanswered tools")
        return self


class AgentFinalDecision(DomainModel):
    action: Literal["WAIT", "OBSERVE", "TRADE"]
    reason: str = Field(min_length=1, max_length=4096)
    intent_ids: tuple[Identifier, ...] = Field(default=(), max_length=8)


class AgentTurnResponse(DomainModel):
    request_id: Identifier
    tool_calls: tuple[ToolCall, ...] = Field(default=(), max_length=8)
    final: AgentFinalDecision | None = None
    usage: ModelUsage
    provider_metadata: ProviderMetadata | None = None
    reasoning_details: tuple[JsonValue, ...] = ()

    @model_validator(mode="after")
    def exclusive_and_bound(self) -> Self:
        if bool(self.tool_calls) == (self.final is not None):
            raise ValueError("agent turn must contain tools or a final result, exclusively")
        if self.usage.request_id != self.request_id:
            raise ValueError("agent usage belongs to another request")
        if len({c.tool_call_id for c in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("duplicate tool call identity")
        bounded_json(self.model_dump(mode="json"), 32768)
        return self


class AgentTurnRecord(DomainModel):
    request: AgentTurnRequest
    response: AgentTurnResponse | None = None
    status: Literal["SENT", "RESPONDED", "RECONCILING"]


class AgentToolRecord(DomainModel):
    call: ToolCall
    result: ToolResult | None = None


AgentRunStatus = Literal[
    "RUNNING", "WAIT", "COMPLETED", "FAILED", "EXPIRED", "INTERRUPTED", "RECONCILING"
]


class AgentRun(DomainModel):
    run_id: Identifier
    request_key: Identifier
    executor_id: Identifier | None = None
    lane: EventAgentLane
    mode: Literal["ANALYSIS", "REVIEW"]
    event: WatchEvent | None = None
    context: AgentContext | None = None
    started_at: UtcDateTime
    deadline: UtcDateTime
    status: AgentRunStatus = "RUNNING"
    revision: Revision = 1
    turns: tuple[AgentTurnRecord, ...] = Field(default=(), max_length=4)
    tools: tuple[AgentToolRecord, ...] = Field(default=(), max_length=8)
    decision: AgentFinalDecision | None = None
    completed_at: UtcDateTime | None = None
    reason: str | None = Field(default=None, max_length=256)
