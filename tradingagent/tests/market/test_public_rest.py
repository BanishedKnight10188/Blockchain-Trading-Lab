"""Public-only host, bounded responses and deterministic rate-limit behavior."""

import asyncio
import gzip
import importlib
from datetime import timedelta

import httpx
import pytest

from agent_platform.adapters.fake.clock import FakeClock
from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS


def rest_module():
    return importlib.import_module("agent_platform.adapters.binance_direct.public_rest")


def row(index=0):
    return [
        MS + index * 60000,
        "100",
        "110",
        "90",
        "105",
        "2",
        MS + (index + 1) * 60000 - 1,
        "202",
        3,
        "1",
        "100",
        "0",
    ]


def test_public_klines_map_exact_owned_events_at_actual_receipt_and_bound_the_query():
    async def exercise():
        clock = FakeClock(NOW + timedelta(seconds=60))
        seen = []

        def respond(request):
            seen.append(request)
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS + 60000})
            clock.advance_to(NOW + timedelta(seconds=61))
            return httpx.Response(200, json=[row(), row(1)])

        async with rest_module().PublicRestClient(
            clock, transport=httpx.MockTransport(respond)
        ) as client:
            events = await client.klines()
        assert [item.payload.is_closed for item in events] == [True, False]
        assert events[0].payload.quote_volume == 202
        assert events[0].received_at == clock.utcnow()
        request = seen[-1]
        assert request.method == "GET"
        assert request.url.host == "data-api.binance.vision"
        assert dict(request.url.params) == {"symbol": "BTCUSDT", "interval": "1m", "limit": "120"}
        assert "X-MBX-APIKEY" not in request.headers
        assert "authorization" not in request.headers
        assert not request.content

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/v3/order", {"symbol": "BTCUSDT"}),
        ("/api/v3/account", {}),
        ("https://evil.example/api/v3/time", {}),
        ("/api/v3/../order", {}),
        ("/api/v3/time?signature=secret", {}),
        ("/api/v3/time", {"signature": "secret"}),
        ("/api/v3/klines", {"symbol": "ETHUSDT", "interval": "1m", "limit": 120}),
        ("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": True}),
        ("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 121}),
    ],
)
def test_unsupported_paths_and_parameters_fail_before_any_transport(path, params):
    async def exercise():
        def forbidden(request):
            pytest.fail("invalid request reached the transport")

        async with rest_module().PublicRestClient(
            FakeClock(NOW), transport=httpx.MockTransport(forbidden)
        ) as client:
            with pytest.raises(ValueError):
                await client.get(path, params)

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [429, 418])
def test_rate_limit_blocks_all_public_endpoints_until_monotonic_retry_after(status):
    async def exercise():
        clock, calls = FakeClock(NOW), []

        def respond(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(
                    status, headers={"Retry-After": "10"}, json={"msg": "sensitive-body"}
                )
            return httpx.Response(200, json={"serverTime": MS + 10000})

        module = rest_module()
        async with module.PublicRestClient(clock, transport=httpx.MockTransport(respond)) as client:
            for _ in range(2):
                with pytest.raises(module.PublicRateLimited) as captured:
                    await client.server_time()
                assert captured.value.retry_after_seconds == 10
                assert "sensitive-body" not in str(captured.value)
            assert len(calls) == 1
            clock.advance_to(NOW + timedelta(seconds=10))
            assert await client.server_time() == clock.utcnow()
            assert len(calls) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="sensitive-body"),
        httpx.Response(302, headers={"Location": "https://evil.example/"}),
        httpx.Response(200, content=b"invalid-secret-json"),
        httpx.Response(200, content=b"x" * 262145),
    ],
)
def test_errors_and_oversized_response_are_sanitized_without_redirect_or_retry(response):
    async def exercise():
        calls = []

        def respond(request):
            calls.append(request)
            return response

        module = rest_module()
        async with module.PublicRestClient(
            FakeClock(NOW), transport=httpx.MockTransport(respond)
        ) as client:
            with pytest.raises(module.PublicRestError) as captured:
                await client.server_time()
            assert "secret" not in str(captured.value)
            assert "sensitive" not in str(captured.value)
            assert "evil" not in str(captured.value)
        assert len(calls) == 1

    asyncio.run(exercise())


def test_timeout_is_sanitized_and_cancellation_is_not_swallowed():
    async def exercise():
        async def respond(request):
            raise httpx.ReadTimeout("private response/credentials", request=request)

        module = rest_module()
        async with module.PublicRestClient(
            FakeClock(NOW), transport=httpx.MockTransport(respond)
        ) as client:
            with pytest.raises(module.PublicRestError) as captured:
                await client.server_time()
            assert "private" not in str(captured.value)

        async def cancelled(request):
            raise asyncio.CancelledError()

        async with module.PublicRestClient(
            FakeClock(NOW), transport=httpx.MockTransport(cancelled)
        ) as client:
            with pytest.raises(asyncio.CancelledError):
                await client.server_time()

    asyncio.run(exercise())


def test_malformed_kline_batch_and_server_time_are_not_partly_returned():
    async def exercise():
        module = rest_module()
        for payload in ([row(), ["bad"]], {"serverTime": True}):
            async with module.PublicRestClient(
                FakeClock(NOW),
                transport=httpx.MockTransport(
                    lambda request, payload=payload: httpx.Response(200, json=payload)
                ),
            ) as client:
                with pytest.raises(module.PublicRestError):
                    if isinstance(payload, list):
                        await client.klines()
                    else:
                        await client.server_time()

    asyncio.run(exercise())


def test_cross_minute_response_delay_cannot_turn_a_sampled_partial_candle_into_final():
    async def exercise():
        clock = FakeClock(NOW + timedelta(seconds=59))

        def respond(request):
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS + 59000})
            clock.advance_to(NOW + timedelta(seconds=61))
            return httpx.Response(200, json=[row()])

        async with rest_module().PublicRestClient(
            clock, transport=httpx.MockTransport(respond)
        ) as client:
            delayed = (await client.klines())[0]
            assert delayed.received_at == NOW + timedelta(seconds=61)
            assert not delayed.payload.is_closed
            confirmed = (await client.klines())[0]
            assert confirmed.payload.is_closed
            assert delayed.event_id != confirmed.event_id

    asyncio.run(exercise())


def test_compressed_response_is_rejected_before_any_decode_or_stream_read():
    async def exercise():
        read = []

        class Compressed(httpx.AsyncByteStream):
            async def __aiter__(self):
                read.append(True)
                yield gzip.compress(b"x" * (8 * 1024 * 1024))

        def respond(request):
            assert request.headers["accept-encoding"] == "identity"
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Compressed())

        module = rest_module()
        async with module.PublicRestClient(
            FakeClock(NOW), transport=httpx.MockTransport(respond)
        ) as client:
            with pytest.raises(module.PublicRestError):
                await client.server_time()
        assert not read

    asyncio.run(exercise())


def test_excessive_json_nesting_is_a_sanitized_protocol_failure():
    async def exercise():
        module = rest_module()
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"[" * 5000 + b"]" * 5000)
        )
        async with module.PublicRestClient(FakeClock(NOW), transport=transport) as client:
            with pytest.raises(module.PublicRestError):
                await client.server_time()

    asyncio.run(exercise())


def test_local_clock_leading_server_cannot_freeze_the_servers_current_candle():
    async def exercise():
        clock = FakeClock(NOW + timedelta(seconds=60.5))

        def respond(request):
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS + 56500})
            return httpx.Response(200, json=[row()])

        async with rest_module().PublicRestClient(
            clock, transport=httpx.MockTransport(respond)
        ) as client:
            server = await client.server_time()
            assert (clock.utcnow() - server).total_seconds() == 4
            current = (await client.klines())[0]
            assert current.received_at == clock.utcnow()
            assert not current.payload.is_closed

    asyncio.run(exercise())
