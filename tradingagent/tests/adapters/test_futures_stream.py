"""Real WebSocket framing with deterministic sockets and first-reception times."""

import asyncio
import json
from datetime import timedelta

import pytest

from agent_platform.adapters.binance_direct.futures_stream import FuturesStreamMarket
from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.futures_values import FuturesQuote
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from tests.domain.test_futures_paper import NOW


def message(kind, at=NOW, **changes):
    ms = int(at.timestamp() * 1000)
    fields = (
        {
            "e": "bookTicker",
            "E": ms,
            "T": ms,
            "s": "BTCUSDT",
            "st": 1,
            "u": 7,
            "b": "1999",
            "a": "2001",
            "B": "3",
            "A": "4",
        }
        if kind == "book"
        else {
            "e": "markPriceUpdate",
            "E": ms,
            "s": "BTCUSDT",
            "st": 1,
            "p": "2000",
            "i": "2000",
            "r": "0.0001",
            "T": ms + 3600000,
        }
    )
    return json.dumps(fields | changes)


@pytest.mark.asyncio
async def test_book_burst_keeps_latest_quote_without_rebuilding_full_history(monkeypatch):
    async with FuturesStreamMarket(FakeClock(NOW)) as market:
        market._symbol = "BTCUSDT"
        market._connected = {"book": True, "mark": True}
        market.accept("book", message("book"), NOW)
        market.accept("mark", message("mark"), NOW)
        original = market.current_snapshot
        builds = []

        def snapshot(symbol):
            builds.append(symbol)
            return original(symbol)

        monkeypatch.setattr(market, "current_snapshot", snapshot)
        for i in range(100):
            market.accept("book", message("book", u=100 + i, b="1998"), NOW)
        assert not builds, "every book update revalidated the whole recent quote history"
        assert original("BTCUSDT").quote.bid == 1998
        with pytest.raises(FuturesMarketUnavailable):
            market.accept("book", message("book", u=200, b="2002"), NOW)


@pytest.mark.asyncio
async def test_two_streams_explicit_proxy_and_disconnect_invalidate_quote():
    queues = {"book": asyncio.Queue(), "mark": asyncio.Queue()}
    calls = []

    class Socket:
        def __init__(self, queue):
            self.queue = queue

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await self.queue.get()
            if isinstance(value, Exception):
                raise value
            return value

    def connect(uri, **options):
        calls.append((uri, options))
        return Socket(queues["book" if "bookTicker" in uri else "mark"])

    clock = FakeClock(NOW)
    async with FuturesStreamMarket(
        clock, proxy_url="http://127.0.0.1:7897", connect=connect
    ) as market:
        await queues["book"].put(message("book"))
        await queues["mark"].put(message("mark"))
        first = await market.snapshot("BTCUSDT")
        assert first.quote.mark == 2000 and len(first.recent_quotes) == 1
        assert any("/public/ws/btcusdt@bookTicker" in uri for uri, _ in calls)
        assert any("/market/ws/btcusdt@markPrice@1s" in uri for uri, _ in calls)
        assert all(options["proxy"] == "http://127.0.0.1:7897" for _, options in calls)
        clock.advance_to(NOW + timedelta(seconds=1))
        second = await market.snapshot("BTCUSDT")
        assert second.quote.received_at == first.quote.received_at  # Never retimestamp a read.
        await queues["book"].put(ConnectionError("fake disconnection"))
        await asyncio.sleep(0.02)
        with pytest.raises(FuturesMarketUnavailable):
            market.current_snapshot("BTCUSDT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"st": 2}, {"s": "ETHUSDT"}, {"E": int(NOW.timestamp() * 1000) + 51}]
)
async def test_wrong_or_future_stream_evidence_is_rejected(changes):
    async with FuturesStreamMarket(FakeClock(NOW)) as market:
        market._symbol = "BTCUSDT"
        with pytest.raises(ValueError):
            market.accept("mark", message("mark", **changes), NOW)


@pytest.mark.asyncio
async def test_small_clock_uncertainty_preserves_raw_event_and_first_reception_times():
    async with FuturesStreamMarket(FakeClock(NOW)) as market:
        market._symbol = "BTCUSDT"
        market._connected = {"book": True, "mark": True}
        ahead = NOW + timedelta(milliseconds=20)
        market.accept("book", message("book", ahead), NOW)
        market.accept("mark", message("mark", ahead), NOW)
        snapshot = market.current_snapshot("BTCUSDT")
        assert snapshot.quote.book_at == snapshot.quote.mark_at == ahead
        assert snapshot.quote.received_at == NOW
        assert market.public_status["clock_uncertainty_ms"] == 50
        assert "clock_evidence" not in snapshot.model_dump(mode="json")
        with pytest.raises(ValueError, match="chronology"):
            FuturesQuote(**(snapshot.quote.model_dump() | {"source": "offline_replay"}))
        with pytest.raises(ValueError, match="future"):
            market.accept("mark", message("mark", NOW + timedelta(milliseconds=51)), NOW)


@pytest.mark.asyncio
async def test_calibration_recovers_future_events_and_expiry_blocks_the_quote():
    from agent_platform.runtime.clock import CalibratedClock, TimeSample

    seconds = [0.0]
    clock = CalibratedClock(wall_now=lambda: NOW, monotonic=lambda: seconds[0])
    clock.update([TimeSample("ntp.aliyun.com", NOW + timedelta(seconds=0.4), 0, 0.02, 0.4)])
    async with FuturesStreamMarket(clock) as market:
        market._symbol = "BTCUSDT"
        market._connected = {"book": True, "mark": True}
        event = NOW + timedelta(seconds=0.38)
        market.accept("book", message("book", event), clock.utcnow())
        market.accept("mark", message("mark", event), clock.utcnow())
        snapshot = market.current_snapshot("BTCUSDT")
        assert snapshot.quote.mark_at == event
        assert snapshot.quote.received_at == NOW + timedelta(seconds=0.4)
        assert snapshot.clock_evidence.source == "ntp.aliyun.com"
        from agent_platform.domain.futures_market import FuturesMarketSnapshot

        stored = snapshot.model_dump_json()
        assert (
            FuturesMarketSnapshot.model_validate_json(stored).clock_evidence
            == snapshot.clock_evidence
        )
        assert market.public_status["clock"]["ready"]
        seconds[0] = 181
        with pytest.raises(ValueError, match="clock_unsynchronized"):
            market.accept("book", message("book", clock.utcnow()), clock.utcnow())
        with pytest.raises(FuturesMarketUnavailable):
            market.current_snapshot("BTCUSDT")


@pytest.mark.asyncio
async def test_uncalibrated_live_clock_cannot_accept_a_fresh_looking_event():
    from agent_platform.runtime.clock import CalibratedClock

    clock = CalibratedClock(wall_now=lambda: NOW, monotonic=lambda: 0)
    async with FuturesStreamMarket(clock) as market:
        market._symbol = "BTCUSDT"
        with pytest.raises(ValueError, match="clock_unsynchronized"):
            market.accept("book", message("book"), NOW)


@pytest.mark.asyncio
async def test_tls_reset_before_connection_made_is_cleanly_closed():
    from websockets.uri import parse_uri

    from agent_platform.adapters.binance_direct.futures_stream import _FuturesConnect

    connector = _FuturesConnect("wss://fstream.binance.com/public/ws/btcusdt@bookTicker")
    connection = connector.protocol_factory(parse_uri(connector.uri))
    connection.connection_lost(ConnectionResetError())
    assert connection.connection_lost_waiter.done()
