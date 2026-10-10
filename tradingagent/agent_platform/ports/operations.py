"""Local operational diagnostics, with no arbitrary text logging interface."""

from typing import Protocol

from agent_platform.domain.operations import OperationalEvent


class OperationalLogPort(Protocol):
    async def write(self, event: OperationalEvent) -> None: ...
