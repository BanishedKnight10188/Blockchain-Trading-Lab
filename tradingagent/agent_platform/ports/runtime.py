"""Local process supervision and query-only diagnostics."""

from typing import Protocol

from agent_platform.domain.runtime_views import DecisionRuntimeView


class RuntimePort(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class DecisionRuntimeStatusPort(Protocol):
    async def latest(self) -> DecisionRuntimeView: ...
