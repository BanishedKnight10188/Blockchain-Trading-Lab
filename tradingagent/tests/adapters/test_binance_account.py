"""AccountPort is bound to one real Spot scope and sanitized owned facts."""

import importlib

import httpx
import pytest

from agent_platform.adapters.binance_direct.read_client import ReadOnlyClient
from agent_platform.adapters.binance_direct.signing import HmacCredentials
from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.account import TradeCursor
from agent_platform.ports.account import AccountPort
from tests.adapters.test_account_mapping import account_wire, order_wire, trade_wire
from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS


def module():
    return importlib.import_module("agent_platform.adapters.binance_direct.account")


@pytest.mark.asyncio
async def test_owned_account_port_uses_only_unsigned_time_and_signed_gets():
    paths = []

    def respond(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": MS})
        assert request.headers["x-mbx-apikey"] == "fake-key"
        data = {
            "/api/v3/account": account_wire(),
            "/api/v3/openOrders": [order_wire()],
            "/api/v3/myTrades": [trade_wire()],
        }
        return httpx.Response(200, json=data[request.url.path])

    clock = FakeClock(NOW)
    async with ReadOnlyClient(
        clock, HmacCredentials("fake-key", "fake-secret"), transport=httpx.MockTransport(respond)
    ) as client:
        adapter = module().BinanceAccount(client, clock, "account-1")
        assert isinstance(adapter, AccountPort)
        assert (await adapter.snapshot("account-1")).status == "fresh"
        assert (await adapter.orders("account-1", "BTCUSDT"))[0].order_id == "123"
        assert (await adapter.trades("account-1", "BTCUSDT", TradeCursor())).trades[
            0
        ].executor == "human"
    assert paths == ["/api/v3/time", "/api/v3/account", "/api/v3/openOrders", "/api/v3/myTrades"]


@pytest.mark.asyncio
async def test_wrong_scope_rejected_without_network():
    def forbidden(request):
        pytest.fail("wrong scope reached network")

    clock = FakeClock(NOW)
    async with ReadOnlyClient(clock, transport=httpx.MockTransport(forbidden)) as client:
        adapter = module().BinanceAccount(client, clock, "account-1")
        for ref, symbol in (
            ("account-2", "BTCUSDT"),
            ("account-1", "ETHUSDT"),
            ("paper:a", "BTCUSDT"),
        ):
            with pytest.raises(ValueError):
                await adapter.orders(ref, symbol)


@pytest.mark.parametrize(
    "status,data,reason",
    [(401, {}, "authentication"), (429, {}, "rate_limit"), (200, {}, "invalid_data")],
)
@pytest.mark.asyncio
async def test_transport_and_mapping_failures_are_port_failures(status, data, reason):
    def respond(request):
        return (
            httpx.Response(200, json={"serverTime": MS})
            if request.url.path == "/api/v3/time"
            else httpx.Response(status, json=data, headers={"Retry-After": "30"})
        )

    clock = FakeClock(NOW)
    async with ReadOnlyClient(
        clock, HmacCredentials("fake-key", "fake-secret"), transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(module().AccountReadUnavailable) as captured:
            await module().BinanceAccount(client, clock, "account-1").snapshot("account-1")
    assert captured.value.reason == reason
    assert "fake" not in str(captured.value)
    if status == 429:
        assert captured.value.retry_after_seconds == 30


@pytest.mark.asyncio
async def test_duplicate_or_terminal_open_orders_are_not_a_valid_open_set():
    for orders in (
        [order_wire(), order_wire()],
        [order_wire(status="FILLED", executedQty="0.002")],
    ):

        def respond(request, orders=orders):
            return httpx.Response(
                200, json={"serverTime": MS} if request.url.path == "/api/v3/time" else orders
            )

        clock = FakeClock(NOW)
        async with ReadOnlyClient(
            clock,
            HmacCredentials("fake-key", "fake-secret"),
            transport=httpx.MockTransport(respond),
        ) as client:
            with pytest.raises(module().AccountReadUnavailable):
                await (
                    module()
                    .BinanceAccount(client, clock, "account-1")
                    .orders("account-1", "BTCUSDT")
                )


@pytest.mark.asyncio
async def test_two_day_ban_remains_typed_and_preserves_retry_delay():
    def respond(request):
        return (
            httpx.Response(200, json={"serverTime": MS})
            if request.url.path == "/api/v3/time"
            else httpx.Response(418, headers={"Retry-After": "172800"})
        )

    clock = FakeClock(NOW)
    async with ReadOnlyClient(
        clock, HmacCredentials("fake-key", "fake-secret"), transport=httpx.MockTransport(respond)
    ) as client:
        adapter = module().BinanceAccount(client, clock, "account-1")
        for _ in range(2):
            with pytest.raises(module().AccountReadUnavailable) as captured:
                await adapter.snapshot("account-1")
            assert (
                captured.value.reason == "rate_limit"
                and captured.value.retry_after_seconds == 172800
            )
