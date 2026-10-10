"""Offline contracts for Binance closed 1m/5m Futures kline transport."""

import importlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from tests.fixtures.watch_cases import NOW, candles


def millis(at):
    return int(at.timestamp() * 1000)


def payload(interval="1m", **changes):
    bar = candles(1, interval=interval)[0]
    value = {
        "e": "kline",
        "s": "BTCUSDT",
        "st": 1,
        "E": millis(NOW),
        "k": {
            "s": "BTCUSDT",
            "i": interval,
            "x": True,
            "t": millis(bar.opened_at),
            "T": millis(bar.closed_at),
            "o": "100",
            "h": "110",
            "l": "90",
            "c": "105",
            "v": "100",
            "q": "10500",
        },
    }
    value.update(changes)
    return value


class Public:
    def __init__(self, bars):
        self.bars, self.queries = bars, []

    async def catalog(self):
        return (SimpleNamespace(symbol="BTCUSDT"),)

    async def get(self, path, params):
        self.queries.append((path, params))
        return [
            [
                millis(c.opened_at),
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
                millis(c.closed_at),
                "10500",
                1,
                "0",
                "0",
                "0",
            ]
            for c in self.bars
        ]


def adapter(public=None, **kwargs):
    cls = importlib.import_module(
        "agent_platform.adapters.binance_direct.watch_data"
    ).BinanceWatchData
    return cls(FakeClock(NOW), public=public or Public(candles()), **kwargs)


@pytest.mark.parametrize("interval", ["1m", "5m"])
def test_closed_kline_contract(interval):
    source = adapter()
    parsed = source.decode(json.dumps(payload(interval)), "BTCUSDT", interval)
    assert parsed.is_closed and parsed.close == 105
    value = payload(interval)
    value["k"]["x"] = False
    assert source.decode(json.dumps(value), "BTCUSDT", interval) is None


@pytest.mark.parametrize(
    "change",
    [
        {"st": 2},
        {"st": True},
        {"s": "ETHUSDT"},
        {"e": "continuous_kline"},
        {"E": millis(NOW) + 1000},
    ],
)
def test_market_and_time_mix_rejected(change):
    with pytest.raises(ValueError):
        adapter().decode(json.dumps(payload(**change)), "BTCUSDT", "1m")


@pytest.mark.asyncio
async def test_rest_history_excludes_open_candle_and_rejects_wrong_contract():
    public = Public(candles())
    snapshot = await adapter(public).latest("BTCUSDT", "1m")
    assert snapshot.features.ema_26 == 105 and snapshot.occurred_at == NOW
    assert public.queries[0][1]["endTime"] == millis(NOW) - 1
    with pytest.raises(ValueError):
        await adapter(public).latest("ETHUSDT", "1m")
    bad = Public(candles(end=NOW + timedelta(minutes=1)))
    with pytest.raises(ValueError):
        await adapter(bad).latest("BTCUSDT", "1m")


@pytest.mark.asyncio
async def test_stream_closed_duplicate_and_conflict_with_bound_subscription():
    raw = payload()

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(json.loads(value))

        def __aiter__(self):
            async def messages():
                yield json.dumps(raw)
                yield json.dumps(raw)
                changed = payload()
                changed["k"]["v"] = "101"
                yield json.dumps(changed)

            return messages()

    socket = Socket()

    class Connect:
        def __init__(self, url, **kwargs):
            assert url == "wss://fstream.binance.com/market/ws"

        async def __aenter__(self):
            return socket

        async def __aexit__(self, *args):
            return False

    source = adapter(connect=Connect)
    stream = source.stream("BTCUSDT", "1m", None)
    seed = await anext(stream)
    conflict = await anext(stream)
    assert seed.effective_quality == "ready"
    assert conflict.effective_quality == "data_conflict"
    assert socket.sent[0]["params"] == ["btcusdt@kline_1m"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_restart_emits_same_cursor_repair_before_socket():
    public = Public(candles())

    class ForbiddenConnect:
        def __init__(self, *args, **kwargs):
            raise AssertionError("repaired cursor should be emitted before opening socket")

    source = adapter(public, connect=ForbiddenConnect)
    snapshot = await source.latest("BTCUSDT", "1m")
    stream = source.stream("BTCUSDT", "1m", snapshot.candle_key)
    repaired = await anext(stream)
    assert repaired.candle_key == snapshot.candle_key
    await stream.aclose()


@pytest.mark.asyncio
async def test_nonconstant_ema_uses_smoothing_across_live_and_restart():
    from decimal import Decimal

    from agent_platform.application.features import ema

    bars = candles(27)
    bars = (
        *bars[:-1],
        bars[-1].model_copy(
            update={
                "open": Decimal("1000"),
                "high": Decimal("1010"),
                "low": Decimal("990"),
                "close": Decimal("1000"),
            }
        ),
    )
    expected = ema(tuple(c.close for c in bars), 26)
    public = Public(bars)
    next_time = NOW + timedelta(minutes=1)
    next_bar = candles(1, end=next_time)[0].model_copy(
        update={
            "open": Decimal("800"),
            "high": Decimal("810"),
            "low": Decimal("790"),
            "close": Decimal("800"),
            "quote_volume": Decimal("10500"),
        }
    )
    incoming = payload(E=millis(next_time))
    incoming["k"].update(
        {
            "t": millis(next_bar.opened_at),
            "T": millis(next_bar.closed_at),
            "o": "800",
            "h": "810",
            "l": "790",
            "c": "800",
        }
    )

    class Socket:
        async def send(self, value):
            assert json.loads(value)["params"] == ["btcusdt@kline_1m"]

        def __aiter__(self):
            async def messages():
                source.clock.advance_to(next_time)
                yield json.dumps(incoming)

            return messages()

    class Connect:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            return False

    source = adapter(public, connect=Connect)
    latest = await source.latest("BTCUSDT", "1m")
    stream = source.stream("BTCUSDT", "1m", None)
    seed = await anext(stream)
    assert seed.features.ema_26 == expected == latest.features.ema_26
    live = await anext(stream)
    public.bars = (*bars, next_bar)
    expected_live = ema(tuple(c.close for c in public.bars), 26)
    assert live.features.ema_26 == expected_live
    await stream.aclose()
    resumed = source.stream("BTCUSDT", "1m", live.candle_key)
    repaired = await anext(resumed)
    assert repaired.features.ema_26 == expected_live
    assert repaired.content_hash == live.content_hash
    await resumed.aclose()
