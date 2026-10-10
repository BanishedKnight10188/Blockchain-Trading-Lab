"""Public history can recover; a possible paid assessment is never repeated."""

import asyncio
import importlib
from datetime import timedelta

import pytest
import pytest_asyncio

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.application.sessions import SessionService
from agent_platform.domain.sessions import TradingStyle
from tests.test_session_market import NOW, domain


@pytest_asyncio.fixture
async def analysis(tmp_path):
    types = importlib.import_module("agent_platform.domain.session_analysis")
    store_type = importlib.import_module(
        "agent_platform.adapters.sqlite.session_analysis"
    ).SqliteInitialAnalysisStore
    service_type = importlib.import_module(
        "agent_platform.application.session_analysis"
    ).InitialAnalysisService
    history_type = importlib.import_module(
        "agent_platform.adapters.fake.session_analysis"
    ).FakeHistoricalMarket
    model_type = importlib.import_module(
        "agent_platform.adapters.fake.session_analysis"
    ).FakeInitialAnalysis
    clock = FakeClock(NOW)
    sessions = SqliteSessionStore(tmp_path / "sessions.sqlite3")
    await sessions.initialize()
    session_service = SessionService(sessions, clock)
    session = await session_service.create(
        TradingStyle(strength=80),
        domain().SessionAnalysisTarget(market="usdt_perpetual", symbol="ETHUSDT"),
    )
    store = store_type(sessions.path)
    await store.initialize()
    provider, model = history_type(clock), model_type()
    svc = service_type(sessions=sessions, store=store, clock=clock, history=provider, model=model)
    return svc, session_service, session, model, provider, types


@pytest.mark.asyncio
async def test_history_is_given_to_flash_once_and_survives_reload(analysis):
    svc, _, session, model, provider, _ = analysis
    await asyncio.gather(svc.step(), svc.step())
    for _ in range(3):
        await svc.public_view()
    record = await svc.store.get(session.session_id)
    assert record.status == "complete" and record.result.source == "fake"
    assert provider.calls == 1 and len(model.requests) == 1
    request = model.requests[0]
    assert request.history.target.symbol == "ETHUSDT" and len(request.history.candles) == 168
    assert request.style.strength == 80 and request.style_revision == 1
    assert "BTC" not in request.model_dump_json() and "account" not in request.model_dump_json()
    other = type(svc)(
        sessions=svc.sessions,
        store=type(svc.store)(svc.store.path),
        clock=svc.clock,
        history=provider,
        model=model,
    )
    await other.step()
    assert (
        len(model.requests) == 1 and (await other.public_view())["record"]["status"] == "complete"
    )


@pytest.mark.asyncio
async def test_missing_flash_still_prepares_real_history_without_call(analysis):
    svc, _, session, model, _, _ = analysis
    svc.model = None
    await svc.step()
    record = await svc.store.get(session.session_id)
    assert record.status == "model_unconfigured" and len(record.history.candles) == 168
    assert record.result is None and not model.requests


@pytest.mark.asyncio
async def test_history_failure_blocks_model_and_is_not_retried_by_read(analysis):
    svc, _, session, model, provider, _ = analysis

    async def bad(target):
        raise importlib.import_module(
            "agent_platform.ports.session_analysis"
        ).HistoricalDataUnavailable("untrusted provider detail")

    provider.history = bad
    await svc.step()
    assert (await svc.store.get(session.session_id)).status == "history_unavailable"
    assert not model.requests
    assert "untrusted provider detail" not in str(await svc.public_view())


@pytest.mark.asyncio
async def test_failed_public_history_recovers_after_durable_backoff_without_model(analysis):
    from agent_platform.ports.session_analysis import HistoricalDataUnavailable

    svc, _, session, model, provider, _ = analysis
    svc.model = None
    original = provider.history
    calls = 0

    async def transient(target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HistoricalDataUnavailable("temporary_public_failure")
        return await original(target)

    provider.history = transient
    await svc.step()
    first = await svc.store.get(session.session_id)
    assert first.status == "history_unavailable"
    # Reloads and runtime restarts must respect the persisted retry deadline.
    view = await svc.public_view()
    assert view["history_retry_at"] == (NOW + timedelta(seconds=60)).isoformat()
    other = type(svc)(
        sessions=svc.sessions,
        store=type(svc.store)(svc.store.path),
        clock=svc.clock,
        history=provider,
    )
    svc.clock.advance_to(NOW + timedelta(seconds=59))
    await other.step()
    assert calls == 1
    svc.clock.advance_to(NOW + timedelta(seconds=60))
    await other.step()
    finished = await svc.store.get(session.session_id)
    assert calls == 2 and finished.status == "model_unconfigured"
    assert len(finished.history.candles) == 168 and not model.requests
    assert finished.request_id == first.request_id
    import sqlite3

    with sqlite3.connect(svc.store.path) as connection:
        row = connection.execute(
            "SELECT body FROM session_initial_analysis_attempts WHERE session_id=?",
            (session.session_id,),
        ).fetchone()
    assert row is not None and '"status":"history_unavailable"' in row[0]
    await other.step()
    assert calls == 2


@pytest.mark.asyncio
async def test_history_retry_can_run_one_first_analysis_but_never_repeats_failed_model(analysis):
    from agent_platform.ports.model import ModelCallFailed
    from agent_platform.ports.session_analysis import HistoricalDataUnavailable

    svc, _, session, model, provider, _ = analysis
    original = provider.history
    calls = 0

    async def transient(target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HistoricalDataUnavailable("temporary_public_failure")
        return await original(target)

    model_calls = 0

    async def failed(request):
        nonlocal model_calls
        model_calls += 1
        raise ModelCallFailed("possible_paid_failure")

    provider.history, model.analyze = transient, failed
    await svc.step()
    assert model_calls == 0
    svc.clock.advance_to(NOW + timedelta(seconds=60))
    await svc.step()
    assert model_calls == 1
    assert (await svc.store.get(session.session_id)).status == "model_failed"
    svc.clock.advance_to(NOW + timedelta(seconds=180))
    await svc.step()
    assert calls == 2 and model_calls == 1
    assert (await svc.public_view())["history_retry_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["style", "pause", "close"])
async def test_history_retry_preserves_style_identity_and_pause(analysis, change):
    from agent_platform.ports.session_analysis import HistoricalDataUnavailable

    svc, sessions, session, model, provider, _ = analysis

    async def failed(target):
        raise HistoricalDataUnavailable("temporary_public_failure")

    provider.history = failed
    await svc.step()
    svc.clock.advance_to(NOW + timedelta(seconds=120))
    if change == "style":
        await sessions.change_style(session.session_id, TradingStyle(strength=10), 1)
    elif change == "pause":
        await sessions.transition(session.session_id, "running", 1)
        await sessions.transition(session.session_id, "paused", 2)
    else:
        await sessions.transition(session.session_id, "closed", 1)
    await svc.step()
    assert (await svc.store.get(session.session_id)).style.strength == 80
    assert (await svc.public_view())["history_retry_at"] is None
    assert not model.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["style", "close", "pause"])
async def test_late_result_is_discarded_after_session_changes(analysis, change):
    svc, sessions, session, model, _, _ = analysis
    entered, release = asyncio.Event(), asyncio.Event()
    original = model.analyze

    async def blocked(request):
        entered.set()
        await release.wait()
        return await original(request)

    model.analyze = blocked
    task = asyncio.create_task(svc.step())
    await entered.wait()
    if change == "style":
        await sessions.change_style(session.session_id, TradingStyle(strength=10), 1)
    elif change == "pause":
        await sessions.transition(session.session_id, "running", 1)
        await sessions.transition(session.session_id, "paused", 2)
    else:
        await sessions.transition(session.session_id, "closed", 1)
    release.set()
    await task
    record = await svc.store.get(session.session_id)
    assert record.status == "discarded" and record.result is not None
    assert (await svc.public_view())["current_result"] is None


@pytest.mark.asyncio
async def test_paused_session_does_not_start_or_expose_initial_analysis(analysis):
    svc, sessions, session, model, provider, _ = analysis
    await sessions.transition(session.session_id, "running", 1)
    await sessions.transition(session.session_id, "paused", 2)
    await svc.step()
    assert await svc.store.get(session.session_id) is None
    assert not model.requests and provider.calls == 0
    assert (await svc.public_view())["reason"] == "paused"


@pytest.mark.asyncio
async def test_second_runtime_cannot_recover_a_live_attempt(analysis):
    from agent_platform.runtime.session_analysis import InitialAnalysisRuntime

    svc, _, session, model, provider, _ = analysis
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = model.analyze

    async def blocked(request):
        entered.set()
        await release.wait()
        result = await original(request)
        return result

    original_finish = svc._finish

    async def completed(*args):
        await original_finish(*args)
        finished.set()

    model.analyze, svc._finish = blocked, completed
    first = InitialAnalysisRuntime(svc)
    other_service = type(svc)(
        sessions=svc.sessions,
        store=type(svc.store)(svc.store.path),
        clock=svc.clock,
        history=provider,
        model=model,
    )
    second = InitialAnalysisRuntime(other_service)
    try:
        await first.start()
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(RuntimeError, match="already owned"):
            await second.start()
        assert (await svc.store.get(session.session_id)).status == "pending"
        release.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert (await svc.store.get(session.session_id)).status == "complete"
    finally:
        release.set()
        await first.stop()
        await second.stop()
    await second.start()
    await second.stop()
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_expired_response_or_mismatched_request_is_not_published(analysis):
    svc, _, session, model, _, types = analysis
    original = model.analyze

    async def late(request):
        response = await original(request)
        svc.clock.advance_to(NOW + timedelta(seconds=16))
        return response

    model.analyze = late
    await svc.step()
    assert (await svc.store.get(session.session_id)).status == "discarded"


@pytest.mark.asyncio
async def test_interrupted_claim_does_not_repeat_possible_billable_attempt(analysis):
    svc, _, session, model, _, _ = analysis
    claimed = await svc.store.claim(session, svc.clock.utcnow())
    assert claimed is not None
    await svc.store.recover(svc.clock.utcnow())
    await svc.step()
    assert not model.requests
    assert (await svc.store.get(session.session_id)).status == "interrupted"


@pytest.mark.asyncio
async def test_changed_style_hides_previously_completed_first_analysis(analysis):
    svc, sessions, session, _, _, _ = analysis
    await svc.step()
    assert (await svc.public_view())["current_result"] is not None
    await sessions.change_style(session.session_id, TradingStyle(strength=1), 1)
    view = await svc.public_view()
    assert view["current_result"] is None and view["reason"] == "style_changed"


@pytest.mark.asyncio
async def test_thirty_day_context_uses_compact_closed_bars(analysis):
    svc, sessions, session, model, _, _ = analysis
    await sessions.transition(session.session_id, "closed", 1)
    selected = await sessions.create(
        TradingStyle(strength=80),
        domain().SessionAnalysisTarget(market="usdt_perpetual", symbol="ETHUSDT", history_days=30),
    )
    await svc.step()
    record = await svc.store.get(selected.session_id)
    assert record.status == "complete"
    assert len(model.requests[0].context_data()["candles"]) == 720


@pytest.mark.asyncio
async def test_wrong_symbol_history_is_never_saved_as_selected_evidence(analysis):
    svc, _, session, model, provider, _ = analysis
    original = provider.history

    async def wrong(target):
        return await original(
            domain().SessionAnalysisTarget(market="usdt_perpetual", symbol="BTCUSDT")
        )

    provider.history = wrong
    await svc.step()
    record = await svc.store.get(session.session_id)
    assert record.status == "history_unavailable" and record.history is None
    assert not model.requests


@pytest.mark.asyncio
async def test_explicit_model_configuration_can_finish_prepared_unbilled_job(analysis):
    svc, _, session, model, _, _ = analysis
    svc.model = None
    await svc.step()
    original = await svc.store.get(session.session_id)
    assert original.status == "model_unconfigured"
    svc.model = model
    await svc.step()
    finished = await svc.store.get(session.session_id)
    assert finished.status == "complete" and finished.request_id == original.request_id
    await svc.step()
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_futures_session_cannot_create_legacy_spot_paper(tmp_path):
    from agent_platform.application.paper_trading import PaperTradingService
    from agent_platform.domain.paper_trading import PaperSettings
    from agent_platform.ports.paper import PaperGuardConflict

    sessions = SqliteSessionStore(tmp_path / "guard.sqlite3")
    await sessions.initialize()
    selected = await SessionService(sessions, FakeClock(NOW)).create(
        TradingStyle(strength=80),
        domain().SessionAnalysisTarget(market="usdt_perpetual", symbol="BTCUSDT"),
    )

    class UnexpectedStore:
        async def create(self, *args, **kwargs):
            pytest.fail("Spot wallet must not be touched")

    svc = PaperTradingService(
        store=UnexpectedStore(),
        sessions=sessions,
        clock=FakeClock(NOW),
        market=None,
        model=None,
        execution=None,
        price_version="fake",
        decision_source="offline_mock",
        market_source="offline_demo",
    )
    with pytest.raises(PaperGuardConflict):
        await svc.configure(
            PaperSettings(
                initial_usdt="1000",
                order_quantity="0.001",
                max_position_quantity="0.01",
                max_run_loss_usdt="20",
                fee_bps="10",
                slippage_bps="1",
                max_price_drift_bps="10",
                min_confidence="0.8",
                strategy_instructions="test",
            ),
            session_id=selected.session_id,
            style_revision=1,
        )
