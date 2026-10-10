"""Latest-frame archiving stays bounded and cannot alter account evidence."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.retention import SqliteMarketArchive
from agent_platform.domain.market import MarketSnapshot
from agent_platform.domain.overview import OverviewFrame
from agent_platform.ports.sessions import PersistenceUnavailable
from agent_platform.runtime.latest import LatestOverview
from tests.adapters.test_retention import record
from tests.domain.test_decisions import NOW


def frame(at=NOW, *, candle_count=1, **changes):
    candles = tuple(
        record(at - timedelta(minutes=candle_count - i - 1), kind="minute").payload
        for i in range(candle_count)
    )
    return OverviewFrame(
        mode="fake",
        market_source="fake",
        captured_at=at,
        market=MarketSnapshot(symbol="BTCUSDT", as_of=at, status="warming", candles=candles),
        **changes,
    )


def project(value):
    return importlib.import_module("agent_platform.application.market_archive").archive_records(
        value
    )


def runtime(cache, archive, clock, **kwargs):
    cls = importlib.import_module("agent_platform.runtime.market_archive").MarketArchiveRuntime
    return cls(cache, archive, clock, **kwargs)


def test_projection_preserves_exact_minutes_and_bounds_old_window():
    values = project(frame(candle_count=130))
    assert len(values) == 121 and values[0].kind == "raw"
    assert values[0].payload.price is None and values[0].payload.status == "warming"
    assert values[-1].payload.close == record().payload.price
    assert values[1].event_at == NOW - timedelta(minutes=119)
    assert "account_ref" not in values[0].model_dump_json()


def test_disabled_and_unavailable_frames_do_not_invent_market_records():
    assert project(OverviewFrame(mode="disabled", captured_at=NOW)) == ()
    assert project(OverviewFrame(mode="fake", market_source="fake", captured_at=NOW)) == ()


@pytest.mark.asyncio
async def test_one_second_sampling_skips_duplicates_and_retains_new_minutes(tmp_path):
    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", market_source="fake")
    archive = SqliteMarketArchive(tmp_path / "market.sqlite3")
    await archive.initialize()
    worker = runtime(cache, archive, clock)
    await cache.publish(frame())
    assert await worker.sample_once() == 2
    assert await worker.sample_once() == 0
    clock.advance_to(NOW + timedelta(milliseconds=500))
    await cache.publish(frame(clock.utcnow()))
    assert await worker.sample_once() == 0
    clock.advance_to(NOW + timedelta(seconds=1))
    await cache.publish(frame(clock.utcnow(), candle_count=0))
    assert await worker.sample_once() == 1
    value = project(await cache.latest())[0]
    assert await archive.get(value.record_id) == value
    assert worker.observations_written == 3 and worker.pending_count == 0


@pytest.mark.asyncio
async def test_slow_archive_has_one_owned_write_and_coalesces_latest_frame(tmp_path):
    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", market_source="fake")
    archive = SqliteMarketArchive(tmp_path / "market.sqlite3")
    started, release = asyncio.Event(), asyncio.Event()
    original = archive.append_many

    async def slow(values):
        started.set()
        await release.wait()
        return await original(values)

    archive.append_many = slow
    await cache.publish(frame())
    worker = runtime(cache, archive, clock)
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        for i in range(1, 30):
            clock.advance_to(NOW + timedelta(seconds=i))
            await cache.publish(frame(clock.utcnow(), candle_count=0))
        assert worker.worker_count == 1 and worker.pending_count == 1
        release.set()
    finally:
        release.set()
        await asyncio.gather(worker.stop(), worker.stop())
    assert worker.worker_count == 0 and worker.pending_count == 0
    assert await archive.get(project(frame())[0].record_id) is not None


@pytest.mark.asyncio
async def test_archive_disk_failure_stops_only_archive_and_hides_exception(tmp_path):
    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", market_source="fake")
    archive = SqliteMarketArchive(tmp_path / "market.sqlite3")

    async def fail(values):
        raise PersistenceUnavailable("private-error-secret")

    archive.append_many = fail
    await cache.publish(frame())
    worker = runtime(cache, archive, clock)
    await worker.start()
    try:
        async with asyncio.timeout(2):
            while worker.last_failure is None:  # noqa: ASYNC110
                await asyncio.sleep(0)
        assert not worker.running and worker.last_failure == "persistence"
        assert (await cache.latest()).market is not None
        assert worker.observations_written == 0
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_retention_cleanup_is_one_bounded_batch_per_minute(tmp_path):
    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", market_source="fake")
    archive = SqliteMarketArchive(tmp_path / "market.sqlite3")
    await archive.initialize()
    expired = tuple(record(NOW - timedelta(days=8, seconds=i)) for i in range(3))
    await archive.append_many(expired)
    worker = runtime(cache, archive, clock, prune_limit=1)
    await worker.sample_once()
    assert worker.observations_removed == 1
    await worker.sample_once()
    assert worker.observations_removed == 1
    clock.advance_to(NOW + timedelta(minutes=1))
    await worker.sample_once()
    assert worker.observations_removed == 2


@pytest.mark.asyncio
async def test_recovered_older_minute_is_saved_and_changed_closed_minute_is_rejected(tmp_path):
    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", market_source="fake")
    archive = SqliteMarketArchive(tmp_path / "market.sqlite3")
    await archive.initialize()
    worker = runtime(cache, archive, clock)
    await cache.publish(frame())
    assert await worker.sample_once() == 2
    clock.advance_to(NOW + timedelta(seconds=1))
    recovered = frame(candle_count=2).model_copy(update={"captured_at": clock.utcnow()})
    await cache.publish(recovered)
    assert await worker.sample_once() == 2  # Current sample plus missing older minute.
    old = project(recovered)[1]
    assert await archive.get(old.record_id) == old
    clock.advance_to(NOW + timedelta(seconds=2))
    candles = list(recovered.market.candles)
    candles[-1] = record(NOW, kind="minute", price="61000").payload
    bad = recovered.model_copy(
        update={
            "captured_at": clock.utcnow(),
            "market": recovered.market.model_copy(update={"candles": tuple(candles)}),
        }
    )
    await cache.publish(bad)
    with pytest.raises(ValueError, match="changed"):
        await worker.sample_once()
    assert (
        await archive.get(project(frame())[-1].record_id)
    ).payload.close == record().payload.price
