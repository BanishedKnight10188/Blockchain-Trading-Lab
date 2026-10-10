"""Stop all local workers even after partial startup or shutdown failure."""

import asyncio

from agent_platform.domain.health import RuntimeHealth, WorkerHealth
from agent_platform.ports.runtime import RuntimePort


class RuntimeSupervisor:
    def __init__(self, *workers: RuntimePort):
        if len(workers) > 9:
            raise ValueError("supervisor supports at most nine owned workers")
        self.workers = workers
        self._lifecycle = asyncio.Lock()
        self._state = "stopped"
        self._failure = None

    @property
    def running(self):
        return any(getattr(worker, "running", False) for worker in self.workers)

    async def start(self):
        async with self._lifecycle:
            if self._state != "stopped":
                raise RuntimeError("supervisor is already started")
            self._state, self._failure = "starting", None
            try:
                for worker in self.workers:
                    await worker.start()
                self._state = "running"
            except BaseException:
                self._failure = "worker_start_failed"
                await self._stop_workers()
                raise

    async def stop(self):
        async with self._lifecycle:
            if self._state == "stopped":
                return
            await self._stop_workers()

    async def _stop_workers(self):
        self._state = "stopping"
        failure = None
        for worker in reversed(self.workers):
            try:
                await worker.stop()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            self._state = "degraded"
            self._failure = "worker_stop_failed"
            raise failure
        self._state = "stopped"
        if self._failure == "worker_stop_failed":
            self._failure = None

    async def health(self) -> RuntimeHealth:
        names = {
            "ReadOnlyRuntime": "data",
            "DecisionRuntime": "decisions",
            "ReviewJobService": "reviews",
            "AccountSignalRuntime": "private_stream",
            "MarketArchiveRuntime": "archive",
            "DiagnosticsRuntime": "diagnostics",
            "PaperTradingRuntime": "paper",
            "FuturesTradingRuntime": "futures",
        }
        codes = {
            "credentials",
            "authentication",
            "rate_limit",
            "transport",
            "invalid_data",
            "persistence",
            "review_jobs_unavailable",
            "reconcile_unavailable",
            "paper_unavailable",
            "futures_unavailable",
        }
        workers = []
        for worker in self.workers:
            error = getattr(worker, "last_failure", None)
            failure = error if error in codes else "worker_unavailable" if error else None
            workers.append(
                WorkerHealth(
                    name=names.get(type(worker).__name__, "worker"),
                    running=bool(getattr(worker, "running", False)),
                    failure=failure,
                )
            )
        state = self._state
        if state == "running" and any(not worker.running or worker.failure for worker in workers):
            state = "degraded"
        return RuntimeHealth(
            status=state, worker_count=len(workers), workers=tuple(workers), failure=self._failure
        )
