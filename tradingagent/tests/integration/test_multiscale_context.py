import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.background import SqliteBackgroundStore
from agent_platform.application.background import MultiScaleContext
from agent_platform.domain.background import BackgroundResult
from agent_platform.domain.multiscale import BACKGROUND_WINDOWS, KlineBuffer
from tests.domain.test_background import answer
from tests.domain.test_multiscale import NOW, window


class Feed:
    def __init__(self):
        self.buffer = KlineBuffer("BTCUSDT", "fake")
        for i, n in (*BACKGROUND_WINDOWS, ("3m", 20), ("1s", 60)):
            self.buffer.seed(window(i, n))
        self.connected, self.failure = True, None

    async def start(self, symbol):
        assert symbol == self.buffer.symbol

    async def aclose(self):
        pass

    def ensure_prime(self):
        pass


@pytest.mark.asyncio
async def test_retired_context_cannot_restart_on_delayed_prepare(tmp_path):
    store = SqliteBackgroundStore(tmp_path / "retired.sqlite3")
    await store.initialize()
    feed = Feed()
    starts = []

    async def start(symbol):
        starts.append(symbol)

    feed.start = start
    closed = []

    async def release_resources():
        closed.append(True)

    ctx = MultiScaleContext(
        clock=FakeClock(NOW), feed=feed, model=None, store=store, on_close=release_resources
    )
    await ctx.prepare(session(), enabled=False)
    await ctx.aclose()
    await ctx.prepare(session(), enabled=True)
    await ctx.aclose()
    assert starts == ["BTCUSDT"]
    assert closed == [True]


def session(revision=1):
    return SimpleNamespace(
        session_id="s1",
        style_revision=revision,
        style=SimpleNamespace(strength=50),
        analysis_target=SimpleNamespace(symbol="BTCUSDT"),
    )


@pytest.mark.asyncio
async def test_changed_background_model_does_not_reuse_previous_model_summary(tmp_path):
    from tests.domain.test_background import request

    store = SqliteBackgroundStore(tmp_path / "changed-model.sqlite3")
    await store.initialize()
    req = request().model_copy(update={"session_id": "s1", "style_revision": 1})
    old = BackgroundResult(
        request=req,
        answer=answer(),
        source="fake",
        model_id="old-background",
        generated_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    await store.prepare(req)
    await store.finish(req, result=old)
    model = SimpleNamespace(settings=SimpleNamespace(model_id="anthropic/claude-haiku-5.5"))
    ctx = MultiScaleContext(clock=FakeClock(NOW), feed=Feed(), model=model, store=store)
    try:
        await ctx.prepare(session(), enabled=False)
        assert ctx.result is None
        assert not ctx.status(session())["ready"]
        assert (await store.latest("s1", 1, "BTCUSDT")).model_id == "old-background"
    finally:
        await ctx.aclose()


@pytest.mark.asyncio
async def test_background_runs_independently_and_persists_full_source(tmp_path):
    clock = FakeClock(NOW)
    store = SqliteBackgroundStore(tmp_path / "bg.sqlite3")
    await store.initialize()
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def analyze(self, request):
            entered.set()
            await release.wait()
            return BackgroundResult(
                request=request,
                answer=answer(),
                source="fake",
                model_id="fake",
                generated_at=clock.utcnow(),
                expires_at=clock.utcnow() + timedelta(minutes=15),
            )

    ctx = MultiScaleContext(clock=clock, feed=Feed(), model=Model(), store=store)
    await ctx.prepare(session(), enabled=True)
    await asyncio.wait_for(entered.wait(), 1)
    assert ctx.status(session())["reason"] == "background_pending"
    with pytest.raises(ValueError, match="background_pending"):
        ctx.snapshot(session())
    release.set()
    await asyncio.wait_for(ctx._job, 1)
    snapshot = ctx.snapshot(session())
    assert len(snapshot.context_data()["fast_1s"]["candles"]) == 60
    saved = await store.latest("s1", 1, "BTCUSDT")
    assert len(saved.request.windows[-1].candles) == 288
    assert ctx.status(session())["ready"]
    assert not ctx.status(session(2))["ready"]
    await ctx.aclose()


@pytest.mark.asyncio
async def test_unconfigured_background_causes_zero_requests(tmp_path):
    store = SqliteBackgroundStore(tmp_path / "bg.sqlite3")
    await store.initialize()
    ctx = MultiScaleContext(clock=FakeClock(NOW), feed=Feed(), model=None, store=store)
    await ctx.prepare(session(), enabled=True)
    assert ctx._job is None
    assert ctx.status(session())["reason"] == "background_model_unconfigured"
    await ctx.aclose()


@pytest.mark.asyncio
async def test_background_provider_failure_keeps_safe_diagnostic_and_stops_retry(tmp_path):
    from agent_platform.domain.model_diagnostics import ModelDiagnostic
    from agent_platform.ports.model import ModelCallFailed

    store = SqliteBackgroundStore(tmp_path / "failed.sqlite3")
    await store.initialize()
    calls = []

    class Model:
        async def analyze(self, request):
            calls.append(request)
            raise ModelCallFailed(
                "provider_http_503", diagnostic=ModelDiagnostic(stage="transport")
            )

    ctx = MultiScaleContext(clock=FakeClock(NOW), feed=Feed(), model=Model(), store=store)
    try:
        await ctx.prepare(session(), enabled=True)
        await ctx._job
        assert ctx.status(session())["failure_detail"]["code"] == "provider_http_503"
        assert ctx.status(session())["failure_detail"]["diagnostic"]["stage"] == "transport"
        await ctx.prepare(session(), enabled=True)
        assert len(calls) == 1
        import sqlite3

        with sqlite3.connect(store.path) as db:
            failure, diagnostic = db.execute(
                "SELECT failure,diagnostic FROM background_records"
            ).fetchone()
        assert failure == "provider_http_503"
        assert '"transport"' in diagnostic
    finally:
        await ctx.aclose()


@pytest.mark.asyncio
async def test_failed_refresh_retains_valid_cache_and_requires_explicit_resume(tmp_path):
    clock = FakeClock(NOW)
    store = SqliteBackgroundStore(tmp_path / "refresh.sqlite3")
    await store.initialize()
    calls = []

    class Model:
        async def analyze(self, request):
            calls.append(request)
            if len(calls) == 2:
                raise ValueError("provider failed")
            return BackgroundResult(
                request=request,
                answer=answer(),
                source="fake",
                model_id="fake",
                generated_at=clock.utcnow(),
                expires_at=clock.utcnow() + timedelta(seconds=900),
            )

    feed = Feed()
    ctx = MultiScaleContext(clock=clock, feed=feed, model=Model(), store=store)
    try:
        await ctx.prepare(session(), enabled=True)
        await ctx._job
        original = ctx.result
        clock.advance_to(NOW + timedelta(seconds=841))
        for i, n in (*BACKGROUND_WINDOWS, ("3m", 20), ("1s", 60)):
            feed.buffer.seed(window(i, n, end=clock.utcnow()))
        await ctx.prepare(session(), enabled=True)
        await ctx._job
        assert ctx.failure == "background_model_failed"
        assert ctx.result == original and ctx.snapshot(session()).background == original
        assert await store.latest("s1", 1, "BTCUSDT") == original
        await ctx.prepare(session(), enabled=True)
        assert len(calls) == 2  # No automatic paid retry after uncertain failure.
        await ctx.prepare(session(), enabled=False)
        await ctx.prepare(session(), enabled=True)
        await ctx._job
        assert len(calls) == 3 and ctx.failure is None
        assert ctx.result.request.request_id != original.request.request_id
    finally:
        await ctx.aclose()
