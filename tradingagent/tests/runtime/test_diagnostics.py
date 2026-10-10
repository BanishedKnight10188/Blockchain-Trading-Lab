"""One local diagnostic worker, safe failure and deterministic lifecycle."""

import asyncio
import importlib
import json

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.operational_log import SafeOperationalLog
from agent_platform.domain.health import RuntimeHealth
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.domain.test_decisions import NOW


def runtime(log, clock):
    cls = importlib.import_module("agent_platform.runtime.diagnostics").DiagnosticsRuntime

    async def health():
        return RuntimeHealth(status="running", worker_count=0, workers=())

    return cls(log, clock, health)


@pytest.mark.asyncio
async def test_owned_diagnostics_writes_safe_start_and_stop_without_task_leaks(tmp_path):
    path = tmp_path / "operations.jsonl"
    worker = runtime(SafeOperationalLog(path), FakeClock(NOW))
    await worker.start()
    with pytest.raises(RuntimeError):
        await worker.start()
    await asyncio.gather(worker.stop(), worker.stop())
    assert not worker.running and worker.worker_count == 0 and worker.last_failure is None
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["runtime_started", "runtime_stopped"]


@pytest.mark.asyncio
async def test_log_storage_failure_is_visible_and_does_not_loop_or_disclose_error():
    class BrokenLog:
        async def write(self, event):
            raise PersistenceUnavailable("private-signature-secret")

    worker = runtime(BrokenLog(), FakeClock(NOW))
    await worker.start()
    try:
        async with asyncio.timeout(2):
            while worker.last_failure is None:  # noqa: ASYNC110
                await asyncio.sleep(0)
        assert worker.last_failure == "persistence" and not worker.running
    finally:
        await worker.stop()
