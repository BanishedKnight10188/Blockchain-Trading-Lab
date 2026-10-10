"""No live sockets: exercise the actual stream/recovery path with fake transport."""

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.ports.market import MarketDataPort
from tests.domain.test_decisions import NOW
from tests.market.test_buffer import minute
from tests.market.test_normalizer import MS, trade


def book(sequence=1):
    return {"u": sequence, "s": "BTCUSDT", "b": "99", "B": "1", "a": "101", "A": "2"}


class Socket:
    def __init__(self, clock, frames):
        self.clock, self.frames = clock, iter(frames)
        self.closed = False

    async def recv(self):
        try:
            at, payload = next(self.frames)
        except StopIteration:
            await asyncio.Event().wait()
            raise AssertionError("unreachable") from None
        if callable(payload):
            payload = await payload()
        self.clock.advance_to(NOW + timedelta(seconds=at))
        if isinstance(payload, BaseException):
            raise payload
        return payload if isinstance(payload, (str, bytes)) else json.dumps(payload)


class Rest:
    def __init__(self, clock, *, missing=False):
        self.clock, self.missing = clock, missing
        self.calls = 0

    async def klines(self, *, end_time_ms=None):
        self.calls += 1
        end = int((self.clock.utcnow() - NOW).total_seconds() // 60)
        if end_time_ms is not None:
            end = min(end, max(0, (end_time_ms - MS + 1) // 60000))
        return tuple(minute(index, at=end * 60) for index in range(end) if not self.missing)

    async def completed_before(self):
        return self.clock.utcnow()


class Harness:
    def __init__(self, *connections, start=60, missing=False):
        self.clock = FakeClock(NOW + timedelta(seconds=start))
        self.rest = Rest(self.clock, missing=missing)
        self.connections = iter(connections)
        self.sockets, self.options, self.delays = [], [], []

    @asynccontextmanager
    async def connect(self, url, **options):
        self.options.append((url, options))
        socket = Socket(self.clock, next(self.connections))
        self.sockets.append(socket)
        try:
            yield socket
        finally:
            socket.closed = True

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.clock.advance_to(self.clock.utcnow() + timedelta(seconds=seconds))
        await asyncio.sleep(0)

    def client(self, **options):
        module = importlib.import_module("agent_platform.adapters.binance_direct.market_stream")
        return module.BinanceMarketStream(
            self.rest, self.clock, connect=self.connect, sleep=self.sleep, **options
        )


def test_market_data_port_bootstraps_before_publication_and_closes_reader_on_exit():
    async def exercise():
        harness = Harness([(61, book()), (61.1, trade(T=MS + 61100, E=MS + 61100))])
        client = harness.client()
        assert isinstance(client, MarketDataPort)
        stream = client.stream(("BTCUSDT",))
        try:
            first, second = await anext(stream), await anext(stream)
            assert (first.payload.kind, second.payload.kind) == ("book", "trade")
            latest = await client.latest("BTCUSDT")
            assert latest.status == "ready"
            assert len(latest.candles) == 1
            assert latest.book_as_of == NOW + timedelta(seconds=61)
            url, options = harness.options[0]
            assert (
                url
                == "wss://data-stream.binance.vision/stream?streams=btcusdt@trade/btcusdt@bookTicker/btcusdt@kline_1m"
            )
            assert options["proxy"] is None
            assert options["ping_interval"] is None
            assert options["max_queue"] == 32
            assert options["max_size"] == 65536
        finally:
            await stream.aclose()
        assert all(socket.closed for socket in harness.sockets)
        assert not client.running
        # The earlier publication at this exact timestamp remains immutable.
        assert await client.latest("BTCUSDT") == latest
        harness.clock.advance_to(harness.clock.utcnow() + timedelta(milliseconds=1))
        assert (await client.latest("BTCUSDT")).status == "gap"

    asyncio.run(exercise())


def test_duplicates_are_not_reemitted_or_used_to_refresh_book_time():
    async def exercise():
        harness = Harness([(61, book()), (62, book()), (62.1, trade(T=MS + 62100, E=MS + 62100))])
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        try:
            await anext(stream)
            assert (await anext(stream)).payload.kind == "trade"
            latest = await client.latest("BTCUSDT")
            assert latest.book_as_of == NOW + timedelta(seconds=61)
            assert client.stats.duplicates == 1
        finally:
            await stream.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        OSError("private endpoint details"),
        {"stream": "!serverShutdown", "data": {"e": "serverShutdown", "E": MS + 61000}},
    ],
)
def test_disconnect_and_server_shutdown_reconnect_and_restore_closed_minutes(failure):
    async def exercise():
        harness = Harness([(61, failure)], [(121, book(2))])
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        try:
            assert (await anext(stream)).payload.kind == "book"
            latest = await client.latest("BTCUSDT")
            assert latest.status == "ready"
            assert len(latest.candles) == 2
            assert harness.delays == [2]
            assert client.stats.connections == 2
            assert client.stats.reconnects == 1
            assert harness.sockets[0].closed
            assert "private" not in str(client.stats)
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_missing_rest_minutes_keep_gap_even_after_new_live_quote():
    async def exercise():
        harness = Harness([(61, OSError("disconnected"))], [(121, book(2))], missing=True)
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        try:
            await anext(stream)
            latest = await client.latest("BTCUSDT")
            assert latest.status == "gap"
            assert not latest.candles
            assert client.stats.reconciliation_failures >= 1
        finally:
            await stream.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "bad_frame", [b"\xff", "{secret-invalid", "x" * 65537], ids=["utf8", "json", "oversized"]
)
def test_bad_frame_recovers_without_emitting_raw_error_or_invalid_event(bad_frame):
    async def exercise():
        harness = Harness([(61, bad_frame)], [(121, book(2))])
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        try:
            assert (await anext(stream)).payload.kind == "book"
            assert client.stats.failures == 1
            assert "secret" not in str(client.stats)
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_bootstrap_queue_is_bounded_and_overflow_recovers_instead_of_silent_drop():
    async def exercise():
        burst = [(61 + index / 1000, book(index + 1)) for index in range(10)]
        harness = Harness(burst, [(121, book(20))])
        client = harness.client(queue_capacity=2)
        stream = client.stream(("BTCUSDT",))
        try:
            result = await anext(stream)
            assert result.payload.kind == "book"
            assert result.stream_sequence == 20
            assert client.stats.queue_high_water <= 2
            assert client.stats.overflows == 1
            assert (await client.latest("BTCUSDT")).status == "ready"
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_cancellation_during_blocked_receive_closes_socket_and_all_child_tasks():
    async def exercise():
        harness = Harness([])
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        task = asyncio.create_task(anext(stream))
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
        assert all(socket.closed for socket in harness.sockets)
        assert not client.running
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]

    asyncio.run(exercise())


def test_wrong_symbols_fail_before_rest_or_websocket_is_contacted():
    async def exercise():
        harness = Harness([])
        client = harness.client()
        with pytest.raises(ValueError):
            await anext(client.stream(("ETHUSDT",)))
        assert harness.rest.calls == 0
        assert not harness.options

    asyncio.run(exercise())


def test_rate_limit_wait_is_respected_by_reconnect_without_blind_http_retry():
    async def exercise():
        from agent_platform.adapters.binance_direct.public_rest import PublicRateLimited

        harness = Harness([], [(121, book())])
        client = harness.client()
        original = harness.rest.klines
        calls = []

        async def limited_once(**query):
            calls.append(harness.clock.utcnow())
            if len(calls) == 1:
                raise PublicRateLimited(10)
            return await original(**query)

        harness.rest.klines = limited_once
        stream = client.stream(("BTCUSDT",))
        try:
            await anext(stream)
            assert harness.delays == [10]
            assert len(calls) == 2
            assert (await client.latest("BTCUSDT")).status == "ready"
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_another_consumer_cannot_compete_for_the_same_buffer():
    async def exercise():
        harness = Harness([(61, book())])
        client = harness.client()
        first = client.stream(("BTCUSDT",))
        try:
            await anext(first)
            second = client.stream(("BTCUSDT",))
            with pytest.raises(RuntimeError):
                await anext(second)
            assert client.running
            assert len(harness.sockets) == 1
        finally:
            await first.aclose()

    asyncio.run(exercise())


def test_cancellation_during_rest_bootstrap_cancels_both_background_tasks():
    async def exercise():
        harness = Harness([])
        rest_cancelled = []

        async def pending_rest(**query):
            try:
                await asyncio.Event().wait()
            finally:
                rest_cancelled.append(True)

        harness.rest.klines = pending_rest
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        task = asyncio.create_task(anext(stream))
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
        assert rest_cancelled
        assert all(socket.closed for socket in harness.sockets)
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]

    asyncio.run(exercise())


def test_next_completed_minute_attempts_gap_recovery_without_a_reconnect():
    async def exercise():
        deliver_next = asyncio.Event()

        async def later_quote():
            await deliver_next.wait()
            return book(2)

        harness = Harness([(121, book()), (182, later_quote)], start=120, missing=True)
        client = harness.client()
        client.buffer.mark_gap("known-missing-minute", NOW + timedelta(seconds=60))
        stream = client.stream(("BTCUSDT",))
        try:
            await anext(stream)
            assert (await client.latest("BTCUSDT")).status == "gap"
            harness.rest.missing = False
            deliver_next.set()
            await anext(stream)
            # The reader captured the later quote before the first publication;
            # advance capture time to see the newly recovered projection.
            harness.clock.advance_to(harness.clock.utcnow() + timedelta(milliseconds=1))
            assert (await client.latest("BTCUSDT")).status == "ready"
            assert len(harness.sockets) == 1
            assert harness.rest.calls == 2
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_long_gap_rest_recovery_requests_120_completed_minutes_without_current_partial():
    async def exercise():
        import httpx

        from agent_platform.adapters.binance_direct.public_rest import PublicRestClient
        from tests.market.test_buffer import book as book_event
        from tests.market.test_public_rest import row

        harness = Harness([], start=10801)

        def respond(request):
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS + 10801000})
            completed = request.url.params.get("endTime") == str(MS + 10800000 - 1)
            start = 60 if completed else 61
            return httpx.Response(200, json=[row(index) for index in range(start, start + 120)])

        async with PublicRestClient(harness.clock, transport=httpx.MockTransport(respond)) as rest:
            client = harness.client()
            client.rest = rest
            client.buffer.mark_gap("long_disconnect", NOW)
            client.buffer.append(book_event(at=10801))
            await client._restore()
            restored = await client.latest("BTCUSDT")
            assert restored.status == "ready"
            assert len(restored.candles) == 120

    asyncio.run(exercise())


def test_consumer_pause_does_not_make_promptly_received_queued_trade_too_late():
    async def exercise():
        release = asyncio.Event()

        async def next_book():
            await release.wait()
            return book(2)

        harness = Harness(
            [
                (57.5, book()),
                (57.6, next_book),
                (57.7, trade(T=MS + 57700, E=MS + 57700)),
                (61.1, book(3)),
            ],
            start=57.5,
        )
        client = harness.client()
        stream = client.stream(("BTCUSDT",))
        try:
            await anext(stream)
            release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            assert (await anext(stream)).stream_sequence == 2
            assert (await anext(stream)).payload.kind == "trade"
            assert (await anext(stream)).stream_sequence == 3
            assert (await client.latest("BTCUSDT")).status == "ready"
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_default_websocket_connector_refuses_handshake_redirects_without_contacting_them():
    async def exercise():
        from websockets.datastructures import Headers
        from websockets.exceptions import InvalidStatus, WebSocketException
        from websockets.http11 import Response

        module = importlib.import_module("agent_platform.adapters.binance_direct.market_stream")
        clock = FakeClock(NOW)
        client = module.BinanceMarketStream(Rest(clock), clock)
        connector = client._connect(module.STREAM_URL, proxy=None)
        result = connector.process_redirect(
            InvalidStatus(Response(302, "Found", Headers({"Location": "wss://evil.example/ws"})))
        )
        assert isinstance(result, WebSocketException)
        assert "evil" not in str(result)
        assert connector.uri == module.STREAM_URL

    asyncio.run(exercise())


def test_rest_recovery_retries_when_server_minute_completes_even_if_local_clock_leads():
    async def exercise():
        release = asyncio.Event()

        async def later():
            await release.wait()
            return book(2)

        harness = Harness([(60.5, book()), (64.001, later)], start=60.5)

        async def server_bound():
            return harness.clock.utcnow() - timedelta(seconds=4)

        harness.rest.completed_before = server_bound
        client = harness.client()
        client.buffer.mark_gap("disconnected", NOW)
        stream = client.stream(("BTCUSDT",))
        try:
            await anext(stream)
            assert (await client.latest("BTCUSDT")).status == "gap"
            release.set()
            await anext(stream)
            assert (await client.latest("BTCUSDT")).status == "ready"
            assert harness.rest.calls == 2
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_time_proof_refresh_uses_capture_time_after_new_quote_arrives():
    async def exercise():
        import httpx

        from agent_platform.adapters.binance_direct.public_rest import PublicRestClient
        from tests.market.test_buffer import book as book_event
        from tests.market.test_public_rest import row

        harness = Harness([], start=70)
        time_requests = []
        client = harness.client()

        def respond(request):
            if request.url.path == "/api/v3/time":
                time_requests.append(True)
                if len(time_requests) == 2:
                    harness.clock.advance_to(NOW + timedelta(seconds=106))
                    client.buffer.append(book_event(at=106))
                return httpx.Response(
                    200,
                    json={
                        "serverTime": MS
                        + int((harness.clock.utcnow() - NOW).total_seconds() * 1000)
                    },
                )
            harness.clock.advance_to(NOW + timedelta(seconds=105))
            return httpx.Response(200, json=[row()])

        async with PublicRestClient(harness.clock, transport=httpx.MockTransport(respond)) as rest:
            client.rest = rest
            await rest.server_time()
            harness.clock.advance_to(NOW + timedelta(seconds=89.9))
            await client._restore()
            result = await client.latest("BTCUSDT")
            assert result.status == "ready"
            assert len(result.candles) == 1
            assert client.stats.reconciliation_failures == 0
            assert client._next_restore_at == NOW + timedelta(seconds=120.001)

    asyncio.run(exercise())
