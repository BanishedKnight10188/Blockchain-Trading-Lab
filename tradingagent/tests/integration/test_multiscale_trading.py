import asyncio
import json

import pytest

from agent_platform.adapters.fake.multiscale import OfflineBackground, OfflineKlines
from agent_platform.adapters.sqlite.background import SqliteBackgroundStore
from agent_platform.application.background import MultiScaleContext
from tests.integration import test_futures_trade_archive as archive_tests

assembled = archive_tests.assembled
configure = archive_tests.configure


@pytest.mark.asyncio
async def test_pause_switch_resume_rejects_delayed_legacy_history(assembled, tmp_path):
    from types import SimpleNamespace

    from agent_platform.application.jev_tasks import JevTaskWorkbench
    from agent_platform.application.sessions import SessionService
    from agent_platform.config import RuntimeConfig
    from agent_platform.domain.jev_tasks import JevTaskCreate
    from agent_platform.web.jev_task_routes import JevInputAction
    from tests.integration.test_futures_trading_core import configured
    from tests.integration.test_jev_tasks import selection

    svc, clock, ctx, _ = assembled
    manager = JevTaskWorkbench(ctx[5], RuntimeConfig(paper=True, paper_mock=True), None)
    await configured(svc)
    await manager.store.initialize()
    row, _ = await manager.store.reserve(JevTaskCreate.model_validate(selection("race", "ETHUSDT")))
    row = row.model_copy(update={"session_id": "s1", "setup": "ready"})
    await manager.store.save(row)
    manager.contexts[row.task_id] = SimpleNamespace(
        sessions=SessionService(svc.sessions, clock), futures_trading=svc, controls=svc.controls
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc.history.history

    async def delayed_history(target):
        entered.set()
        await release.wait()
        return await original(target)

    svc.history.history = delayed_history
    prediction = asyncio.create_task(svc.step())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        view = await manager.view(row.task_id)
        paused = await manager.pause(
            row.task_id, view["session"]["revision"], view["account"]["revision"]
        )
        changed = await manager.update_context(
            row.task_id,
            JevInputAction(
                confirmed=True,
                expected_revision=paused["account"]["revision"],
                session_revision=paused["session"]["revision"],
                context_revision=0,
                context_mode="multiscale",
            ),
        )
        await manager.start(
            row.task_id, changed["session"]["revision"], changed["account"]["revision"]
        )
        release.set()
        assert await asyncio.wait_for(prediction, 1) is None
        assert svc.model.calls == 0
    finally:
        release.set()
        await asyncio.gather(prediction, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_six_layers_reach_jev_and_strict_fill_archive(assembled, tmp_path):
    svc, clock, _, _ = assembled
    store = SqliteBackgroundStore(tmp_path / "background.sqlite3")
    await store.initialize()
    svc.multiscale = MultiScaleContext(
        clock=clock, feed=OfflineKlines(clock), model=OfflineBackground(clock), store=store
    )
    try:
        run = await configure(svc)
        session = await svc.sessions.active()
        await svc.multiscale.prepare(session, enabled=True)
        await asyncio.wait_for(svc.multiscale._job, 1)
        svc.model.choices = ("OPEN_LONG_M20_L10",)
        cycle = await svc.step()
        assert cycle.status == "filled"
        record = (await svc.archive.page(run.scope.account_ref)).entries[0].record
        state = json.loads(record.execution_command.decision_evidence.request.state_json)
        request = record.execution_command.decision_evidence.request
        assert len(request.model_dump_json().encode()) + 1024 + request.max_output_tokens < 32000
        assert state["market_context"]["version"] == "jev-six-layer-v1"
        assert len(state["market_context"]["short_3m"]["candles"]) == 20
        assert len(state["market_context"]["fast_1s"]["candles"]) == 60
        assert "history" not in state and "recent_quote_ticks" not in state
        assert (await svc.public_view())["multiscale_context"]["ready"]
    finally:
        await svc.multiscale.aclose()


@pytest.mark.asyncio
async def test_missing_background_does_not_dispatch_jev(assembled, tmp_path):
    svc, clock, _, _ = assembled
    store = SqliteBackgroundStore(tmp_path / "background.sqlite3")
    await store.initialize()
    svc.multiscale = MultiScaleContext(
        clock=clock, feed=OfflineKlines(clock), model=None, store=store
    )
    calls = []

    async def forbidden(request):
        calls.append(request)
        raise AssertionError("must not dispatch")

    try:
        await configure(svc)
        svc.model.decide = forbidden
        assert await svc.step() is None
        assert svc.last_failure == "background_model_unconfigured"
        assert calls == []
    finally:
        await svc.multiscale.aclose()
