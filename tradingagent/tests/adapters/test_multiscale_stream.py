import asyncio
import json
from datetime import timedelta

import pytest

from agent_platform.adapters.binance_direct.multiscale import NativeFuturesKlines
from agent_platform.adapters.fake.clock import FakeClock
from tests.domain.test_multiscale import NOW


def frame(interval="1s", *, symbol="BTCUSDT", end=NOW, closed=True):
    seconds = 1 if interval == "1s" else 180
    ms = int(end.timestamp() * 1000)
    data = {
        "e": "continuous_kline" if interval == "1s" else "kline",
        "E": ms,
        "ps": symbol,
        "s": symbol,
        "ct": "PERPETUAL",
        "k": {
            "i": interval,
            "t": ms - seconds * 1000,
            "T": ms - 1,
            "o": "2000",
            "h": "2001",
            "l": "1999",
            "c": "2000",
            "v": "10",
            "q": "20000",
            "x": closed,
        },
    }
    return json.dumps(data)


@pytest.mark.asyncio
async def test_unrelated_invalid_frame_does_not_erase_valid_second_history():
    clock = FakeClock(NOW - timedelta(seconds=60))
    queue = asyncio.Queue()
    processed = asyncio.Event()

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, value):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await queue.get()
            if value is None:
                processed.set()
                return await asyncio.Future()
            clock.advance_to(value[0])
            return value[1]

    client = NativeFuturesKlines(clock, public=None, connect=lambda *a, **k: Socket())
    await client.start("BTCUSDT")
    try:
        for i in range(60):
            end = NOW - timedelta(seconds=59 - i)
            queue.put_nowait((end, frame(end=end)))
        queue.put_nowait((NOW, frame("3m", symbol="ETHUSDT", closed=False)))
        queue.put_nowait(None)
        await asyncio.wait_for(processed.wait(), 2)
        window = client.buffer.window("1s", 60, NOW)
        assert window.complete and window.fresh
        assert client.rejected_frames == 1
    finally:
        await client.aclose()


def test_native_continuous_seconds_and_closed_minute_frame_validation():
    client = NativeFuturesKlines(FakeClock(NOW), public=None)
    client.select("BTCUSDT")
    assert client.accept(frame())
    assert client.accept(frame("3m"))
    assert not client.accept(frame(closed=False))
    with pytest.raises(ValueError):
        client.accept(frame(symbol="ETHUSDT"))
    assert len(client.buffer.window("1s", 60, NOW).candles) == 1


def test_exact_subscriptions_and_late_future_frame_rejected():
    client = NativeFuturesKlines(FakeClock(NOW), public=None)
    client.select("BTCUSDT")
    assert "btcusdt_perpetual@continuousKline_1s" in client.subscriptions
    assert "btcusdt@kline_3m" in client.subscriptions
    assert len(client.subscriptions) == 6
    with pytest.raises(ValueError):
        client.accept(frame(end=NOW + timedelta(seconds=1)))


@pytest.mark.asyncio
async def test_rest_preload_never_queries_seconds():
    calls = []

    class Public:
        async def get(self, path, params):
            calls.append((path, params))
            ms = int(NOW.timestamp() * 1000)
            return [
                [
                    ms - 180000,
                    "2000",
                    "2001",
                    "1999",
                    "2000",
                    "10",
                    ms - 1,
                    "20000",
                    1,
                    "4",
                    "8000",
                    "0",
                ]
            ]

    client = NativeFuturesKlines(FakeClock(NOW), public=Public())
    client.select("BTCUSDT")
    result = await client.load_window("3m", 20)
    assert len(result.candles) == 1 and not result.complete
    assert calls[0][1]["interval"] == "3m"
    with pytest.raises(ValueError):
        await client.load_window("1s", 60)
    assert len(calls) == 1
