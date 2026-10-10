"""Current WebSocket API subscriptions only hint REST reconciliation, never trade."""

import asyncio
import hashlib
import hmac
import importlib
import json
import logging
from contextlib import asynccontextmanager

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidHandshake, InvalidStatus
from websockets.http11 import Response

from agent_platform.adapters.binance_direct.read_client import ReadRateLimited
from agent_platform.adapters.binance_direct.signing import HmacCredentials
from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.ports.account import AccountReadUnavailable
from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS

_CREDENTIALS = HmacCredentials("fake-key'quoted", "fake-secret")


def module():
    return importlib.import_module("agent_platform.adapters.binance_direct.user_stream")


class Rest:
    def __init__(self):
        self.calls = []

    async def request(self, method, path):
        self.calls.append((method, path))
        assert (method, path) == ("GET", "/api/v3/time")
        return {"serverTime": MS - 4000}


class Socket:
    def __init__(self, frames, logger):
        self.frames, self.logger = iter(frames), logger
        self.sent, self.closed = [], False

    async def send(self, frame):
        self.logger.debug("private frame %s", frame)
        self.sent.append(json.loads(frame))

    async def recv(self):
        try:
            result = next(self.frames)
        except StopIteration:
            await asyncio.Event().wait()
        if isinstance(result, BaseException):
            raise result
        return result if isinstance(result, (str, bytes)) else json.dumps(result)


class Harness:
    def __init__(self, frames):
        self.frames, self.rest, self.clock = frames, Rest(), FakeClock(NOW)
        self.socket, self.options = None, None

    @asynccontextmanager
    async def connect(self, url, **options):
        assert url == "wss://ws-api.binance.com:443/ws-api/v3"
        self.options = options
        self.socket = Socket(self.frames, options["logger"])
        try:
            yield self.socket
        finally:
            self.socket.closed = True

    def client(self, credentials=_CREDENTIALS):
        return module().BinanceUserStream(
            self.rest, self.clock, "account-1", credentials, connect=self.connect
        )


def ack(**changes):
    return {
        "id": "read-only-subscription",
        "status": 200,
        "result": {"subscriptionId": 0},
        **changes,
    }


def event(kind, **changes):
    return {"subscriptionId": 0, "event": {"e": kind, "E": MS, **changes}}


@pytest.mark.asyncio
async def test_signature_subscription_uses_server_time_and_raw_sorted_ws_payload(caplog):
    harness = Harness([ack(), event("executionReport", s="BTCUSDT", t=1)])
    caplog.set_level(logging.DEBUG)
    stream = harness.client().updates()
    first = await anext(stream)
    assert first.kind == "reconcile_required"
    second = await anext(stream)
    assert second.kind == "execution_changed" and second.account_ref == "account-1"
    packet = harness.socket.sent[0]
    assert packet["method"] == "userDataStream.subscribe.signature"
    assert set(packet["params"]) == {"apiKey", "timestamp", "recvWindow", "signature"}
    params = packet["params"]
    assert params["timestamp"] == MS - 4000 and params["recvWindow"] == 5000
    payload = "&".join(
        f"{key}={value}" for key, value in sorted(params.items()) if key != "signature"
    )
    assert (
        params["signature"]
        == hmac.new(b"fake-secret", payload.encode(), hashlib.sha256).hexdigest()
    )
    assert "fake" not in caplog.text and params["signature"] not in caplog.text
    assert harness.options["proxy"] is None and harness.options["compression"] is None
    assert harness.options["max_size"] == 262144
    await stream.aclose()
    assert harness.socket.closed


@pytest.mark.asyncio
async def test_missing_credentials_never_opens_socket_or_requests_clock():
    harness = Harness([])
    with pytest.raises(AccountReadUnavailable) as captured:
        await anext(harness.client(None).updates())
    assert captured.value.reason == "credentials"
    assert harness.socket is None and not harness.rest.calls


@pytest.mark.parametrize(
    "frame",
    [
        ack(status=401),
        ack(id="other"),
        ack(result={"subscriptionId": True}),
        "[" * 5000 + "]" * 5000,
        "x" * 262145,
    ],
    ids=["auth", "identity", "boolean", "deep-json", "oversize"],
)
@pytest.mark.asyncio
async def test_bad_ack_is_sanitized_and_does_not_retry_or_send_other_methods(frame):
    harness = Harness([frame])
    with pytest.raises(AccountReadUnavailable) as captured:
        await anext(harness.client().updates())
    assert "fake" not in str(captured.value)
    assert harness.socket.closed and len(harness.socket.sent) == 1


@pytest.mark.asyncio
async def test_balance_changes_are_hints_and_other_symbols_cannot_create_btc_trade():
    harness = Harness(
        [
            ack(),
            event("executionReport", s="ETHUSDT"),
            event("outboundAccountPosition"),
            event("balanceUpdate"),
        ]
    )
    stream = harness.client().updates()
    await anext(stream)
    for _ in range(2):
        signal = await anext(stream)
        assert signal.kind == "balance_changed"
        assert "balances" not in signal.model_dump() and "trade" not in signal.model_dump()
    await stream.aclose()


@pytest.mark.parametrize(
    "frame",
    [
        event("executionReport", s="BTCUSDT", subscriptionId=3),
        {"subscriptionId": 3, "event": {"e": "balanceUpdate"}},
        OSError("fake-key'quoted"),
    ],
)
@pytest.mark.asyncio
async def test_bad_scope_or_connection_failure_leaves_rest_available(frame):
    harness = Harness([ack(), frame])
    stream = harness.client().updates()
    await anext(stream)
    if isinstance(frame, dict) and frame.get("subscriptionId") == 0:
        # A field inside the event does not replace the wrapper's identity.
        assert (await anext(stream)).kind == "execution_changed"
        await stream.aclose()
    else:
        with pytest.raises(AccountReadUnavailable):
            await anext(stream)
        assert harness.socket.closed
    assert (await harness.rest.request("GET", "/api/v3/time"))["serverTime"] > 0


@pytest.mark.asyncio
async def test_cancellation_closes_socket_and_cannot_disable_rest():
    harness = Harness([ack()])
    stream = harness.client().updates()
    await anext(stream)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.socket.closed
    assert (await harness.rest.request("GET", "/api/v3/time"))["serverTime"] > 0


def test_default_transport_refuses_redirects_or_arbitrary_urls():
    connect = module()._UserConnect
    with pytest.raises(ValueError):
        connect("wss://evil.example/")
    instance = connect("wss://ws-api.binance.com:443/ws-api/v3")
    assert isinstance(instance.process_redirect(OSError("private")), InvalidHandshake)


@pytest.mark.asyncio
async def test_subscription_rate_limit_prevents_a_second_handshake_until_retry_after():
    harness = Harness([ack(status=429, error={"code": -1003, "retryAfter": MS - 4000 + 90000})])
    client = harness.client()
    for _ in range(2):
        with pytest.raises(AccountReadUnavailable) as captured:
            await anext(client.updates())
        assert captured.value.reason == "rate_limit"
        assert captured.value.retry_after_seconds == 90
    assert len(harness.rest.calls) == 1 and len(harness.socket.sent) == 1


@pytest.mark.asyncio
async def test_auth_failure_freezes_subscription_only_and_preserves_rest():
    harness = Harness([ack(status=401)])
    client = harness.client()
    for _ in range(2):
        with pytest.raises(AccountReadUnavailable) as captured:
            await anext(client.updates())
        assert captured.value.reason == "authentication"
    assert len(harness.rest.calls) == 1
    await harness.rest.request("GET", "/api/v3/time")


@pytest.mark.asyncio
async def test_http_handshake_rate_limit_is_typed_and_stops_the_next_open():
    harness = Harness([])
    opened = []

    @asynccontextmanager
    async def fail(url, **options):
        opened.append(url)
        response = Response(429, "Too Many Requests", Headers({"Retry-After": "90"}))
        mapped = module()._UserConnect(url).process_redirect(InvalidStatus(response))
        raise mapped
        yield

    client = module().BinanceUserStream(
        harness.rest, harness.clock, "account-1", _CREDENTIALS, connect=fail
    )
    for _ in range(2):
        with pytest.raises(AccountReadUnavailable) as captured:
            await anext(client.updates())
        assert captured.value.reason == "rate_limit" and captured.value.retry_after_seconds == 90
    assert len(opened) == 1 and not harness.rest.calls


@pytest.mark.asyncio
async def test_clock_sync_rate_limit_stops_reopening_private_socket():
    harness = Harness([])

    async def fail(method, path):
        raise ReadRateLimited(90)

    harness.rest.request = fail
    client = harness.client()
    first_socket = None
    for _ in range(2):
        with pytest.raises(AccountReadUnavailable) as captured:
            await anext(client.updates())
        assert captured.value.reason == "rate_limit" and captured.value.retry_after_seconds == 90
        if first_socket is None:
            first_socket = harness.socket
        assert harness.socket is first_socket
    assert first_socket.closed
