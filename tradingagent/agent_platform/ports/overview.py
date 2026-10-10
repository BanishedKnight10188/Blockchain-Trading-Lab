"""Latest-only observation transport, independent of Web and concrete storage."""

from typing import Protocol

from agent_platform.domain.overview import OverviewFrame


class OverviewPort(Protocol):
    async def latest(self) -> OverviewFrame: ...

    async def wait(self, after_id: str) -> OverviewFrame: ...
