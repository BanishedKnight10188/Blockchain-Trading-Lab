"""Private stream hints are coalesced and can only request authoritative REST sync."""

import asyncio
import importlib
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.sync import AccountSignal
from agent_platform.ports.account import AccountReadUnavailable
from tests.domain.test_decisions import NOW


def signal(**updates):
    return AccountSignal(
        event_id="signal-1",
        account_ref="local-spot",
        kind="balance_changed",
        received_at=NOW,
        **updates,
    )


def runtime(source, callback, clock, *, wait=None):
    cls = importlib.import_module("agent_platform.runtime.account_signals").AccountSignalRuntime
    return cls(source, callback, clock, account_ref="local-spot", wait=wait)


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():  # noqa: ASYNC110 - bounded test observation
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_many_hints_and_duplicates_use_one_pending_slot_and_safe_callback():
    clock, requests, delivered = FakeClock(NOW), [], asyncio.Event()

    class Source:
        async def updates(self):
            for i in range(200):
                yield signal().model_copy(update={"event_id": "hint-" + str(i)})
            for _ in range(200):
                yield signal().model_copy(update={"event_id": "hint-199"})
            delivered.set()
            await asyncio.Event().wait()

    worker = runtime(Source(), lambda: requests.append("REST reconcile"), clock)
    await worker.start()
    try:
        await delivered.wait()
        await until(lambda: len(requests) == 1)
        assert worker.pending_count == 0 and worker.received_count == 200
        assert worker.last_failure is None
    finally:
        await worker.stop()
    assert not worker.running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update", [{"account_ref": "other"}, {"received_at": NOW + timedelta(seconds=1)}]
)
async def test_invalid_scope_or_future_hints_never_request_sync(update):
    requests, clock = [], FakeClock(NOW)

    class Source:
        async def updates(self):
            yield signal().model_copy(update=update)

    worker = runtime(Source(), lambda: requests.append(1), clock)
    await worker.start()
    try:
        await until(lambda: worker.last_failure is not None)
        assert not requests and worker.last_failure == "invalid_data"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_authentication_rejection_freezes_stream_and_does_not_disable_rest():
    calls, waits, requests = [], [], []

    class Source:
        async def updates(self):
            calls.append(1)
            raise AccountReadUnavailable("authentication")
            yield

    async def wait(delay):
        waits.append(delay)
        await asyncio.Event().wait()

    worker = runtime(Source(), lambda: requests.append(1), FakeClock(NOW), wait=wait)
    await worker.start()
    try:
        await until(lambda: worker.last_failure == "authentication")
        assert calls == [1] and not waits and not requests
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_reconnect_honors_retry_after_and_fixed_failure_without_exception_text():
    attempts, delays, resumed = [], [], asyncio.Event()

    class Source:
        async def updates(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise AccountReadUnavailable("rate_limit", retry_after_seconds=37)
            resumed.set()
            await asyncio.Event().wait()
            yield

    async def wait(delay):
        delays.append(delay)

    worker = runtime(Source(), lambda: None, FakeClock(NOW), wait=wait)
    await worker.start()
    try:
        await resumed.wait()
        assert delays == [37] and len(attempts) == 2
        assert worker.reconnect_count == 1 and worker.last_failure is None
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_stop_cancels_stream_and_debounce_without_orphan_tasks():
    closed = asyncio.Event()

    class Source:
        async def updates(self):
            try:
                await asyncio.Event().wait()
                yield signal()
            finally:
                closed.set()

    worker = runtime(Source(), lambda: None, FakeClock(NOW))
    await worker.start()
    await asyncio.sleep(0)
    await asyncio.gather(worker.stop(), worker.stop())
    assert closed.is_set() and worker.pending_count == 0 and not worker.running


@pytest.mark.asyncio
async def test_late_burst_waits_full_minimum_interval_and_coalesces_while_waiting():
    clock, requests, first, waiting, release = (
        FakeClock(NOW),
        [],
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    delays = []

    class Source:
        async def updates(self):
            yield signal()
            await first.wait()
            for i in range(200):
                yield signal().model_copy(update={"event_id": "late-" + str(i)})
            await asyncio.Event().wait()

    def callback():
        requests.append(clock.monotonic())
        first.set()

    async def wait(delay):
        delays.append(delay)
        waiting.set()
        await release.wait()

    worker = runtime(Source(), callback, clock, wait=wait)
    await worker.start()
    try:
        await waiting.wait()
        assert requests == [0] and delays == [15] and worker.pending_count == 1
        clock.advance_to(NOW + timedelta(seconds=15))
        release.set()
        await until(lambda: len(requests) == 2)
        assert requests == [0, 15] and worker.pending_count == 0
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_account_runtime_reconcile_signal_does_not_mutate_cached_balances():
    from agent_platform.runtime.latest import LatestOverview
    from agent_platform.runtime.read_only import ReadOnlyRuntime
    from tests.application.test_queries import report

    clock = FakeClock(NOW)
    cache = LatestOverview(clock, mode="fake", account_source="fake")

    class Sync:
        calls = 0

        async def sync(self, ref, symbol):
            self.calls += 1
            return report()

    sync = Sync()
    reader = ReadOnlyRuntime(cache, clock, account_sync=sync, account_ref="local-spot")
    await reader.start()
    try:
        await until(lambda: sync.calls == 1)
        for _ in range(100):
            reader.request_sync()
        await until(lambda: sync.calls == 2)
        assert reader.reconcile_pending_count == 0
        await reader.sample_once()
        assert (await cache.latest()).sync.account == report().account
    finally:
        await reader.stop()


@pytest.mark.asyncio
async def test_synchronous_stream_factory_failure_is_classified():
    class Source:
        def updates(self):
            raise AccountReadUnavailable("authentication")

    worker = runtime(Source(), lambda: None, FakeClock(NOW))
    await worker.start()
    try:
        await asyncio.sleep(0)
        assert worker.last_failure == "authentication"
    finally:
        await worker.stop()
