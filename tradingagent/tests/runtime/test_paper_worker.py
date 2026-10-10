"""Paper worker owns its task, pauses on startup/exit and recovers funds."""

import asyncio
import importlib

import pytest

from tests.adapters.test_paper_store import context as _context
from tests.application.test_paper_trading import service

context = _context


@pytest.mark.asyncio
async def test_worker_runs_independently_and_pauses_on_exit(context):
    module = importlib.import_module("agent_platform.runtime.paper_trading")
    svc, _, _, _ = service(context)
    worker = module.PaperTradingRuntime(svc, interval_seconds=0.01)
    await worker.start()
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    for _ in range(200):
        if await svc.store.recent(account.account_ref):
            break
        await asyncio.sleep(0.01)
    await worker.stop()
    assert not worker.running
    assert (await svc.store.latest()).status == "paused"
    assert (await svc.store.recent(account.account_ref))[0].status != "pending"


@pytest.mark.asyncio
async def test_stop_still_owns_task_when_storage_is_unavailable(context):
    from agent_platform.ports.sessions import PersistenceUnavailable

    module = importlib.import_module("agent_platform.runtime.paper_trading")
    svc, _, _, _ = service(context)
    worker = module.PaperTradingRuntime(svc)
    await worker.start()

    async def unavailable(*args):
        raise PersistenceUnavailable("offline fault")

    svc.store.latest = unavailable
    svc.store.recover = unavailable
    with pytest.raises(PersistenceUnavailable):
        await worker.stop()
    assert not worker.running
