"""Private reads remain constrained even with a key capable of trading."""

import asyncio
import hashlib
import hmac
import importlib
import logging
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from httpcore import RemoteProtocolError
from httpcore._trace import Trace

from agent_platform.adapters.binance_direct.signing import HmacCredentials
from agent_platform.adapters.fake.clock import FakeClock
from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS

KEY, SECRET = "fake-private-api-key", "fake-private-secret"


def module():
    return importlib.import_module("agent_platform.adapters.binance_direct.read_client")


@pytest.mark.parametrize(
    "method,path,params",
    [
        ("POST", "/api/v3/order", {"symbol": "BTCUSDT"}),
        ("DELETE", "/api/v3/order", {"symbol": "BTCUSDT", "orderId": 1}),
        ("PUT", "/api/v3/account", {}),
        ("GET", "/sapi/v1/capital/withdraw/apply", {}),
        ("GET", "/sapi/v1/asset/transfer", {}),
        ("GET", "https://evil.example/api/v3/account", {}),
        ("GET", "/api/v3/account?signature=secret", {}),
        ("GET", "/api/v3/account", {"timestamp": MS}),
        ("GET", "/api/v3/account", {"signature": "injected"}),
        ("GET", "/api/v3/myTrades", {"symbol": "ETHUSDT"}),
        ("GET", "/api/v3/myTrades", {"symbol": "BTCUSDT", "fromId": True}),
        ("GET", "/api/v3/myTrades", {"symbol": "BTCUSDT", "limit": 1001}),
        ("GET", "/api/v3/myTrades", {"symbol": "BTCUSDT", "fromId": 1, "startTime": MS}),
        ("GET", "/api/v3/myTrades", {"symbol": "BTCUSDT", "orderId": 1, "endTime": MS}),
        ("GET", "/api/v3/order", {"symbol": "BTCUSDT"}),
    ],
)
def test_write_paths_arbitrary_urls_and_unowned_parameters_fail_before_network(
    method, path, params
):
    async def exercise():
        def forbidden(request):
            pytest.fail("unsupported request reached network, including clock sync")

        async with module().ReadOnlyClient(
            FakeClock(NOW), HmacCredentials(KEY, SECRET), transport=httpx.MockTransport(forbidden)
        ) as client:
            with pytest.raises(ValueError):
                await client.request(method, path, params)

    asyncio.run(exercise())


def test_signed_get_uses_server_clock_and_exact_wire_signature_with_no_body():
    async def exercise():
        seen = []

        def respond(request):
            seen.append(request)
            assert request.method == "GET"
            assert request.url.host == "api.binance.com"
            assert not request.content
            if request.url.path == "/api/v3/time":
                assert "x-mbx-apikey" not in request.headers
                return httpx.Response(200, json={"serverTime": MS - 4000})
            assert request.headers["x-mbx-apikey"] == KEY
            query, supplied = request.url.query.rsplit(b"&signature=", 1)
            assert hmac.new(SECRET.encode(), query, hashlib.sha256).hexdigest() == supplied.decode()
            values = parse_qs(query.decode())
            assert values["timestamp"] == [str(MS - 4000)]
            assert values["recvWindow"] == ["5000"]
            assert values["symbol"] == ["BTCUSDT"]
            return httpx.Response(200, json=[])

        async with module().ReadOnlyClient(
            FakeClock(NOW), HmacCredentials(KEY, SECRET), transport=httpx.MockTransport(respond)
        ) as client:
            assert await client.request("GET", "/api/v3/openOrders", {"symbol": "BTCUSDT"}) == []
        assert len(seen) == 2

    asyncio.run(exercise())


def test_missing_credentials_prevents_private_reads_but_public_clock_still_works():
    async def exercise():
        seen = []

        def respond(request):
            seen.append(request.url.path)
            return httpx.Response(200, json={"serverTime": MS})

        source = module()
        async with source.ReadOnlyClient(
            FakeClock(NOW), transport=httpx.MockTransport(respond)
        ) as client:
            with pytest.raises(source.CredentialsUnavailable):
                await client.request("GET", "/api/v3/account")
            assert (await client.request("GET", "/api/v3/time"))["serverTime"] == MS
        assert seen == ["/api/v3/time"]

    asyncio.run(exercise())


@pytest.mark.parametrize("status,code", [(401, -2015), (403, -2015), (400, -1022)])
def test_authentication_errors_freeze_private_retries_and_keep_public_independent(status, code):
    async def exercise():
        seen = []

        def respond(request):
            seen.append(request.url.path)
            return (
                httpx.Response(200, json={"serverTime": MS})
                if request.url.path == "/api/v3/time"
                else httpx.Response(status, json={"code": code, "msg": KEY + SECRET})
            )

        source = module()
        async with source.ReadOnlyClient(
            FakeClock(NOW), HmacCredentials(KEY, SECRET), transport=httpx.MockTransport(respond)
        ) as client:
            for _ in range(2):
                with pytest.raises(source.AuthenticationRejected) as captured:
                    await client.request("GET", "/api/v3/account")
                assert KEY not in str(captured.value)
                assert SECRET not in str(captured.value)
            await client.request("GET", "/api/v3/time")
        assert seen.count("/api/v3/account") == 1
        assert seen.count("/api/v3/time") == 2

    asyncio.run(exercise())


def test_rate_limit_blocks_all_requests_without_an_automatic_retry():
    async def exercise():
        clock, calls = FakeClock(NOW), []

        def respond(request):
            calls.append(request)
            return (
                httpx.Response(429, headers={"Retry-After": "10"})
                if len(calls) == 1
                else httpx.Response(200, json={"serverTime": MS + 10000})
            )

        source = module()
        async with source.ReadOnlyClient(
            clock, HmacCredentials(KEY, SECRET), transport=httpx.MockTransport(respond)
        ) as client:
            for _ in range(2):
                with pytest.raises(source.ReadRateLimited) as captured:
                    await client.request("GET", "/api/v3/time")
                assert captured.value.retry_after_seconds == 10
            assert len(calls) == 1
            clock.advance_to(NOW + timedelta(seconds=10))
            await client.request("GET", "/api/v3/time")
        assert len(calls) == 2

    asyncio.run(exercise())


def test_http_logs_do_not_expose_key_secret_or_signed_url(caplog):
    async def exercise():
        signatures = []

        def respond(request):
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS})
            signatures.append(request.url.params["signature"])
            return httpx.Response(200, json={"balances": []})

        caplog.set_level(logging.DEBUG)
        async with module().ReadOnlyClient(
            FakeClock(NOW), HmacCredentials(KEY, SECRET), transport=httpx.MockTransport(respond)
        ) as client:
            await client.request("GET", "/api/v3/account")
        assert KEY not in caplog.text
        assert SECRET not in caplog.text
        assert all(value not in caplog.text for value in signatures)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "key", ["fake'key", 'fake"key', "fake\\'key"], ids=["single", "double", "slash"]
)
def test_trace_exception_redacts_key_regardless_of_repr_quote_choice(caplog, key):
    async def exercise():
        def respond(request):
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS})
            Trace("test", logging.getLogger("httpcore.http11")).trace(
                "test.failed", {"exception": RemoteProtocolError('header "x" invalid for ' + key)}
            )
            return httpx.Response(200, json={"balances": []})

        caplog.set_level(logging.DEBUG)
        async with module().ReadOnlyClient(
            FakeClock(NOW), HmacCredentials(key, SECRET), transport=httpx.MockTransport(respond)
        ) as client:
            await client.request("GET", "/api/v3/account")
        for variant in (
            key,
            key.replace("\\", "\\\\").replace("'", "\\'"),
            key.replace("\\", "\\\\").replace('"', '\\"'),
        ):
            assert variant not in caplog.text
        assert "[REDACTED]" in caplog.text

    asyncio.run(exercise())


def test_network_errors_hide_signed_request_and_preserve_cancellation():
    async def exercise():
        source = module()

        async def respond(request):
            if request.url.path == "/api/v3/time":
                return httpx.Response(200, json={"serverTime": MS})
            raise httpx.ConnectError(KEY + SECRET + str(request.url), request=request)

        async with source.ReadOnlyClient(
            FakeClock(NOW), HmacCredentials(KEY, SECRET), transport=httpx.MockTransport(respond)
        ) as client:
            with pytest.raises(source.ReadOnlyError) as captured:
                await client.request("GET", "/api/v3/account")
            assert KEY not in str(captured.value)
            assert "signature" not in str(captured.value)

        async def cancelled(request):
            raise asyncio.CancelledError()

        async with source.ReadOnlyClient(
            FakeClock(NOW), transport=httpx.MockTransport(cancelled)
        ) as client:
            with pytest.raises(asyncio.CancelledError):
                await client.request("GET", "/api/v3/time")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://evil.example/"}),
        httpx.Response(200, content=b"x" * 1048577),
        httpx.Response(200, content=b"[" * 5000 + b"]" * 5000),
        httpx.Response(200, text="private-invalid-json"),
    ],
)
def test_bad_private_transport_protocol_is_sanitized_and_never_retried(response):
    async def exercise():
        calls = []

        def respond(request):
            calls.append(request)
            return response

        source = module()
        async with source.ReadOnlyClient(
            FakeClock(NOW), transport=httpx.MockTransport(respond)
        ) as client:
            with pytest.raises(source.ReadOnlyError) as captured:
                await client.request("GET", "/api/v3/time")
            assert "private" not in str(captured.value)
            assert "evil" not in str(captured.value)
        assert len(calls) == 1

    asyncio.run(exercise())
