"""Public chart intervals are independent from the session's model evidence."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient
from agent_platform.adapters.fake.clock import FakeClock
from tests.test_session_market import NOW, contract, row


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,seconds", [("1m", 60), ("5m", 300), ("15m", 900), ("4h", 14400)])
async def test_chart_uses_selected_native_interval_and_excludes_open_bar(interval, seconds):
    from agent_platform.domain.futures_chart import ChartRequest

    at = NOW + timedelta(seconds=10)
    clock = FakeClock(at)
    opened = (
        NOW.replace(hour=4, minute=0)
        if interval == "4h"
        else NOW.replace(minute={"1m": 29, "5m": 25, "15m": 15}[interval])
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET" and "authorization" not in request.headers
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract()]})
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": int(at.timestamp() * 1000)})
        assert request.url.params["interval"] == interval
        assert "startTime" not in request.url.params, "chart must request latest bars"
        values = []
        for i in range(2):
            bar = row(opened + timedelta(seconds=i * seconds))
            bar[6] = bar[0] + seconds * 1000 - 1
            values.append(bar)
        return httpx.Response(200, json=values)

    async with FuturesPublicClient(clock, transport=httpx.MockTransport(handler)) as client:
        request = ChartRequest(symbol="ETHUSDT", interval=interval, limit=1)
        history = await client.chart(request)
        assert len(history.candles) == 1 and history.interval == interval
        assert history.candles[0].closed_at < NOW
        assert history.source == "binance_futures_public"
        assert await client.chart(request) == history
        assert calls == ["/fapi/v1/exchangeInfo", "/fapi/v1/time", "/fapi/v1/klines"]


@pytest.mark.asyncio
async def test_monthly_chart_uses_calendar_months_not_thirty_day_bars():
    from agent_platform.domain.futures_chart import ChartRequest

    def handler(request):
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract()]})
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": int(NOW.timestamp() * 1000)})
        values = []
        for month in (8, 9, 10):
            bar = row(datetime(2026, month, 1, tzinfo=UTC))
            bar[6] = int(datetime(2026, month + 1, 1, tzinfo=UTC).timestamp() * 1000) - 1
            values.append(bar)
        return httpx.Response(200, json=values)

    async with FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        history = await client.chart(ChartRequest(symbol="ETHUSDT", interval="1M", limit=2))
    assert [c.opened_at.month for c in history.candles] == [8, 9]
    assert history.candles[0].closed_at.day == 31


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["duplicate", "gap", "wrong_close"])
async def test_invalid_chart_history_is_never_accepted(failure):
    from agent_platform.domain.futures_chart import ChartRequest
    from agent_platform.ports.session_analysis import HistoricalDataUnavailable

    def handler(request):
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract()]})
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": int(NOW.timestamp() * 1000)})
        start = NOW.replace(minute=0) - timedelta(hours=3)
        values = [row(start + timedelta(hours=i)) for i in range(3)]
        if failure == "duplicate":
            values[1] = values[0]
        elif failure == "gap":
            values.pop(1)
        else:
            values[1][6] += 1
        return httpx.Response(200, json=values)

    async with FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HistoricalDataUnavailable):
            await client.chart(ChartRequest(symbol="ETHUSDT", interval="1h", limit=3))


def test_one_second_is_not_misrepresented_as_native_futures_history():
    from pydantic import ValidationError

    from agent_platform.domain.futures_chart import ChartRequest

    with pytest.raises(ValidationError):
        ChartRequest(symbol="BTCUSDT", interval="1s")


@pytest.mark.asyncio
@pytest.mark.parametrize("recovered", [True, False])
async def test_public_connection_failure_retries_once_before_returning_data(recovered):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1 or not recovered:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(200, json={"serverTime": int(NOW.timestamp() * 1000)})

    async with FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        if recovered:
            result = await client.get("/fapi/v1/time")
            assert result["serverTime"] == int(NOW.timestamp() * 1000)
        else:
            from agent_platform.ports.session_analysis import HistoricalDataUnavailable

            with pytest.raises(HistoricalDataUnavailable):
                await client.get("/fapi/v1/time")
    assert calls == 2


@pytest.mark.asyncio
async def test_failed_proxy_tunnel_is_closed_before_retry_to_avoid_exhausting_pool():
    class PoisonedProxy(httpx.AsyncBaseTransport):
        calls = 0
        poisoned = False
        closes = 0

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                self.poisoned = True
                raise httpx.ConnectError("TLS tunnel ended during handshake", request=request)
            if self.poisoned:
                raise httpx.PoolTimeout("failed tunnel still occupies pool", request=request)
            return httpx.Response(200, json={"serverTime": int(NOW.timestamp() * 1000)})

        async def aclose(self):
            self.closes += 1
            self.poisoned = False

    proxy = PoisonedProxy()
    async with FuturesPublicClient(FakeClock(NOW), transport=proxy) as client:
        old_client = client._client
        result = await client.get("/fapi/v1/time")
        assert result["serverTime"] == int(NOW.timestamp() * 1000)
        assert old_client.is_closed
        assert proxy.closes == 1
    assert proxy.calls == 2
    assert proxy.closes == 2
