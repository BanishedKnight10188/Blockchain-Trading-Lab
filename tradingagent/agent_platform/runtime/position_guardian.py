import asyncio

from agent_platform.domain.position_protection import ProtectionStatus
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.sessions import PersistenceUnavailable


class PositionGuardianRuntime:
    def __init__(self, guardian, scope):
        self.guardian, self.scope, self._task = guardian, scope, None
        self.status = None
        self.last_failure = None

    async def _loop(self):
        while True:
            try:
                async with asyncio.timeout(5):
                    self.status = await self.guardian.step(self.scope)
                self.last_failure = (
                    None if self.status.status != "degraded" else "guardian_unavailable"
                )
            except (
                ValueError,
                OSError,
                TimeoutError,
                PersistenceUnavailable,
                FuturesMarketUnavailable,
            ):
                self.status = ProtectionStatus(status="degraded", reason="guardian_unavailable")
                self.last_failure = "guardian_unavailable"
            await asyncio.sleep(1)

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
