"""Initial history/model work owns its task and never waits on JEV or Spot."""

import asyncio

from agent_platform.ports.sessions import PersistenceUnavailable


class InitialAnalysisRuntime:
    def __init__(self, service):
        self.service, self._task, self.last_failure = service, None, None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def start(self):
        if self.running:
            raise RuntimeError("initial analysis already running")
        await self.service.store.acquire_owner()
        try:
            await self.service.store.recover(self.service.clock.utcnow())
            self._task = asyncio.create_task(self._run(), name="initial-history-analysis")
            await asyncio.sleep(0)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        task, self._task = self._task, None
        try:
            if task:
                task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
        finally:
            await self.service.store.release_owner()

    async def _run(self):
        while True:
            try:
                await self.service.step()
                self.last_failure = None
            except PersistenceUnavailable:
                self.last_failure = "persistence"
                return
            except (ValueError, RuntimeError):
                self.last_failure = "invalid_data"
                return
            await asyncio.sleep(1)
