"""Assembly cleans all partially started workers before leaving its context."""

import asyncio
import importlib

import pytest


@pytest.mark.asyncio
async def test_partial_start_failure_closes_started_and_failing_workers():
    module = importlib.import_module("agent_platform.runtime.supervisor")
    events = []

    class Worker:
        def __init__(self, name, fail=False):
            self.name, self.fail, self.running = name, fail, False

        async def start(self):
            self.running = True
            events.append("start:" + self.name)
            if self.fail:
                raise RuntimeError("failed startup")

        async def stop(self):
            self.running = False
            events.append("stop:" + self.name)

    first, second = Worker("read"), Worker("decision", True)
    runtime = module.RuntimeSupervisor(first, second)
    with pytest.raises(RuntimeError, match="failed startup"):
        await runtime.start()
    assert events == ["start:read", "start:decision", "stop:decision", "stop:read"]
    assert not first.running and not second.running


@pytest.mark.asyncio
async def test_second_start_cannot_shutdown_existing_workers_and_concurrent_stop_is_once():
    module = importlib.import_module("agent_platform.runtime.supervisor")
    events = []

    class Worker:
        running = False

        async def start(self):
            if self.running:
                raise RuntimeError("already started")
            self.running = True
            events.append("start")

        async def stop(self):
            events.append("stop")
            await asyncio.sleep(0)
            self.running = False

    worker = Worker()
    supervisor = module.RuntimeSupervisor(worker)
    await supervisor.start()
    with pytest.raises(RuntimeError):
        await supervisor.start()
    assert worker.running and events == ["start"]
    await asyncio.gather(supervisor.stop(), supervisor.stop())
    assert events == ["start", "stop"] and not supervisor.running
    assert (await supervisor.health()).status == "stopped"


@pytest.mark.asyncio
async def test_health_is_bounded_fixed_diagnostics_and_failure_degrades():
    module = importlib.import_module("agent_platform.runtime.supervisor")

    class Worker:
        running = False
        last_failure = "private-key-never-emit"

        async def start(self):
            self.running = True

        async def stop(self):
            self.running = False

    supervisor = module.RuntimeSupervisor(Worker())
    await supervisor.start()
    try:
        status = await supervisor.health()
        assert status.status == "degraded" and status.worker_count == 1
        assert "private-key" not in status.model_dump_json()
        assert status.workers[0].failure == "worker_unavailable"
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_failed_stop_keeps_degraded_state_and_can_retry_cleanup():
    module = importlib.import_module("agent_platform.runtime.supervisor")

    class Worker:
        running = False
        stop_calls = 0

        async def start(self):
            self.running = True

        async def stop(self):
            self.stop_calls += 1
            if self.stop_calls == 1:
                raise OSError("private-path-do-not-display")
            self.running = False

    worker = Worker()
    supervisor = module.RuntimeSupervisor(worker)
    await supervisor.start()
    with pytest.raises(OSError):
        await supervisor.stop()
    health = await supervisor.health()
    assert health.status == "degraded" and health.failure == "worker_stop_failed"
    assert "private-path" not in health.model_dump_json()
    with pytest.raises(RuntimeError):
        await supervisor.start()
    await supervisor.stop()
    assert worker.stop_calls == 2 and not worker.running
    assert (await supervisor.health()).status == "stopped"
