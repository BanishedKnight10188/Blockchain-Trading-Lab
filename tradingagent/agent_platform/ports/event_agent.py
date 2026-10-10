from datetime import datetime
from typing import Protocol

from agent_platform.domain.agent_events import WatchEvent
from agent_platform.domain.agent_tools import ToolCall, ToolResult
from agent_platform.domain.event_agent import (
    AgentContext,
    AgentFinalDecision,
    AgentRun,
    AgentTurnRequest,
    AgentTurnResponse,
    EventAgentLane,
)


class RunAlreadyStarted(ValueError):
    """Another caller already owns this run's immutable context."""


class AgentRunStorePort(Protocol):
    async def save_lane(self, lane: EventAgentLane, expected_revision: int) -> EventAgentLane: ...
    async def lane(self, lane_id: str) -> EventAgentLane: ...
    async def claim(
        self,
        event: WatchEvent,
        lane: EventAgentLane,
        at: datetime,
        *,
        executor_id: str | None = None,
    ) -> AgentRun: ...
    async def claim_analysis(
        self,
        lane: EventAgentLane,
        request_id: str,
        at: datetime,
        *,
        executor_id: str | None = None,
    ) -> AgentRun: ...
    async def set_context(self, run_id: str, context: AgentContext) -> AgentRun: ...
    async def record_turn(
        self,
        run_id: str,
        request: AgentTurnRequest,
        response: AgentTurnResponse | None,
        status: str,
    ) -> AgentRun: ...
    async def begin_tool(self, run_id: str, call: ToolCall) -> AgentRun: ...
    async def record_tool(self, run_id: str, call: ToolCall, result: ToolResult) -> AgentRun: ...
    async def finish(self, run_id: str, decision: AgentFinalDecision, at: datetime) -> AgentRun: ...
    async def abort(self, run_id: str, status: str, reason: str, at: datetime) -> AgentRun: ...
    async def get(self, run_id: str) -> AgentRun: ...
    async def recover(self, lane_id: str, at: datetime) -> tuple[AgentRun, ...]: ...
