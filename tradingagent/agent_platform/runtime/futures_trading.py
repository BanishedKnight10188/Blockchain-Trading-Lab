"""Fixed dispatch cadence with bounded inference; account execution stays serial."""

import asyncio
import math


class FuturesTradingRuntime:
    def __init__(self, service, *, maintenance_seconds=1, decision_seconds=1, max_predictions=3):
        for value in (maintenance_seconds, decision_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("worker cadence must be finite and positive")
        if service.decision_source == "real_jev" and decision_seconds < 1:
            raise ValueError("real model dispatch minimum is one second")
        if type(max_predictions) is not int or not 1 <= max_predictions <= 3:
            raise ValueError("prediction capacity must be 1..3")
        self.service = service
        self.maintenance_seconds, self.decision_seconds = maintenance_seconds, decision_seconds
        self.max_predictions = max_predictions
        self._tasks = ()
        self._predictions = set()
        self._fault = asyncio.Event()
        self._cadence_changed = asyncio.Event()
        self._owns = False
        self.last_failure = None
        self.metrics = {
            "dispatch_attempts": 0,
            "completed_cycles": 0,
            "skipped_capacity": 0,
            "in_flight": 0,
            "last_cycle_ms": None,
        }
        service.runtime_metrics = self.metrics
        service.cadence_runtime = self

    def set_decision_interval(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 10:
            raise ValueError("decision interval must be 1..10 seconds")
        if self.decision_seconds != seconds:
            self.decision_seconds = seconds
            self._cadence_changed.set()

    async def _wait_dispatch(self, due):
        delay = max(0, due - asyncio.get_running_loop().time())
        if delay:
            try:
                await asyncio.wait_for(self._cadence_changed.wait(), delay)
            except TimeoutError:
                return True
            return False
        return not self._cadence_changed.is_set()

    @property
    def running(self):
        return len(self._tasks) == 2 and all(not t.done() for t in self._tasks)

    async def start(self):
        if self._tasks:
            raise RuntimeError("futures workers already started")
        await self.service.store.acquire_owner()
        self._owns = True
        try:
            await self.service.recover()
            self.service.ready = True
            self._tasks = (
                asyncio.create_task(self._maintenance(), name="futures-maintenance"),
                asyncio.create_task(self._decisions(), name="futures-jev-decisions"),
            )
        except BaseException:
            await self.service.store.release_owner()
            self._owns = False
            raise

    async def stop(self):
        if not self._owns:
            return
        self.service.ready = False
        tasks, self._tasks = self._tasks, ()
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.service.recover()
        finally:
            await self.service.store.release_owner()
            self._owns = False

    async def _cancel_predictions(self):
        tasks = tuple(self._predictions)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._predictions.clear()
        self.metrics["in_flight"] = 0

    async def _predict(self):
        began = asyncio.get_running_loop().time()
        try:
            cycle = await self.service.step()
            if cycle is not None:
                self.metrics["completed_cycles"] += 1
                self.metrics["last_cycle_ms"] = round(
                    (asyncio.get_running_loop().time() - began) * 1000, 1
                )
            self.last_failure = self.service.last_failure
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_failure = self.service.last_failure = "futures_unavailable"
            self._fault.set()

    def _finished(self, task):
        self._predictions.discard(task)
        self.metrics["in_flight"] = len(self._predictions)

    async def _decisions(self):
        loop = asyncio.get_running_loop()
        due = loop.time()
        try:
            while True:
                if self._cadence_changed.is_set():
                    self._cadence_changed.clear()
                    due = loop.time()
                if self._fault.is_set():
                    await self._cancel_predictions()
                    try:
                        await self.service.recover()
                    except Exception:
                        self.last_failure = self.service.last_failure = "persistence"
                    self._fault.clear()
                last_dispatch = getattr(self.service, "_last_model_dispatch", None)
                if self.service.decision_source == "real_jev" and last_dispatch is not None:
                    # Rebase before preparing the next request. Variable funds/
                    # storage work must not shorten actual paid dispatch spacing.
                    due = max(due, last_dispatch + self.decision_seconds)
                    if not await self._wait_dispatch(due):
                        continue
                if len(self._predictions) < self.max_predictions:
                    task = asyncio.create_task(self._predict(), name="futures-prediction")
                    self._predictions.add(task)
                    task.add_done_callback(self._finished)
                    self.metrics["dispatch_attempts"] += 1
                    self.metrics["in_flight"] = len(self._predictions)
                else:
                    self.metrics["skipped_capacity"] += 1
                due += self.decision_seconds
                if due <= loop.time():
                    due = loop.time() + self.decision_seconds
                await self._wait_dispatch(due)
        finally:
            await self._cancel_predictions()

    async def _maintenance(self):
        while True:
            try:
                await self.service.maintain()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_failure = self.service.last_failure = "futures_unavailable"
                self._fault.set()
            await asyncio.sleep(self.maintenance_seconds)
