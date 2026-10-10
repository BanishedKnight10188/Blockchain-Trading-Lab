from typing import Protocol

from agent_platform.domain.event_agent import AgentTurnRequest, AgentTurnResponse


class AgentModelPort(Protocol):
    paid: bool

    async def turn(self, request: AgentTurnRequest) -> AgentTurnResponse: ...
