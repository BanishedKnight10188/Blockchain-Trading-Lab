"""One native kline subscription socket plus bounded public history preloads."""

import asyncio
import json
from datetime import timedelta

from agent_platform.domain.market import Candle
from agent_platform.domain.multiscale import (
    BACKGROUND_WINDOWS,
    SECONDS,
    CandleWindow,
    KlineBuffer,
    boundary,
)

from .futures_public import _millis
from .futures_stream import _FuturesConnect


class NativeFuturesKlines:
    def __init__(self, clock, *, public, proxy_url=None, connect=_FuturesConnect):
        self.clock, self.public, self.proxy, self.connect = clock, public, proxy_url, connect
        self.buffer = None
        self.connected = False
        self.failure = None
        self.rejected_frames = 0
        self._task = self._prime_task = None
        self._next_prime = 0.0

    def select(self, symbol):
        if self.buffer and self.buffer.symbol == symbol:
            return
        if self._task is not None:
            raise ValueError("close the old kline subscription before changing symbol")
        self.buffer = KlineBuffer(symbol, "binance_futures_public")

    @property
    def subscriptions(self):
        symbol = self.buffer.symbol.lower()
        return [f"{symbol}_perpetual@continuousKline_1s"] + [
            f"{symbol}@kline_{interval}" for interval in ("3m", "1d", "4h", "1h", "5m")
        ]

    async def load_window(self, interval, count):
        if interval == "1s" or interval not in SECONDS:
            raise ValueError("REST does not provide native second history")
        if type(count) is not int or not 1 <= count <= 288:
            raise ValueError("invalid history size")
        now = self.clock.utcnow()
        end = boundary(now, interval)
        rows = await self.public.get(
            "/fapi/v1/klines",
            {
                "symbol": self.buffer.symbol,
                "interval": interval,
                "limit": count,
                "endTime": int(end.timestamp() * 1000) - 1,
            },
        )
        if type(rows) is not list or len(rows) > count:
            raise ValueError("invalid native history response")
        bars = []
        for row in rows:
            if type(row) is not list or len(row) != 12:
                raise ValueError("invalid native history bar")
            bars.append(
                Candle(
                    symbol=self.buffer.symbol,
                    opened_at=_millis(row[0]),
                    closed_at=_millis(row[6]),
                    open=row[1],
                    high=row[2],
                    low=row[3],
                    close=row[4],
                    volume=row[5],
                    quote_volume=row[7],
                )
            )
        window = CandleWindow(
            symbol=self.buffer.symbol,
            interval=interval,
            source="binance_futures_public",
            captured_at=self.clock.utcnow(),
            requested_count=count,
            candles=tuple(bars),
        )
        if bars and bars[-1].closed_at >= end:
            raise ValueError("history extends past the completed boundary")
        self.buffer.seed(window)
        return window

    async def prime(self):
        results = await asyncio.gather(
            *(
                self.load_window(interval, count)
                for interval, count in (("3m", 20), *BACKGROUND_WINDOWS)
            ),
            return_exceptions=True,
        )
        self.failure = (
            "kline_history_unavailable" if any(isinstance(r, Exception) for r in results) else None
        )
        self._next_prime = self.clock.monotonic() + 60

    def accept(self, raw):
        if type(raw) is not str or len(raw.encode()) > 65536:
            raise ValueError("invalid kline frame size")
        value = json.loads(raw)
        if type(value) is not dict:
            raise ValueError("invalid kline frame")
        if value.get("result", "missing") is None and value.get("id") == 1:
            return False
        event, bar = value.get("e"), value.get("k")
        if type(bar) is not dict or bar.get("i") not in SECONDS:
            raise ValueError("unsupported kline event")
        interval = bar["i"]
        if (
            value.get("st", 1) != 1
            or (
                interval == "1s"
                and (
                    event != "continuous_kline"
                    or value.get("ps") != self.buffer.symbol
                    or value.get("ct") != "PERPETUAL"
                )
            )
            or (interval != "1s" and (event != "kline" or value.get("s") != self.buffer.symbol))
            or type(bar.get("x")) is not bool
        ):
            raise ValueError("wrong native kline identity")
        received, at = self.clock.utcnow(), _millis(value["E"])
        calibration = getattr(self.clock, "public_status", None)
        if calibration and not calibration["ready"]:
            raise ValueError("kline_clock_unsynchronized")
        if not -timedelta(milliseconds=50) <= received - at <= timedelta(seconds=5):
            raise ValueError("future or stale native kline event")
        if not bar["x"]:
            return False
        candle = Candle(
            symbol=self.buffer.symbol,
            opened_at=_millis(bar["t"]),
            closed_at=_millis(bar["T"]),
            open=bar["o"],
            high=bar["h"],
            low=bar["l"],
            close=bar["c"],
            volume=bar["v"],
            quote_volume=bar.get("q"),
        )
        self.buffer.accept(
            CandleWindow(
                symbol=self.buffer.symbol,
                interval=interval,
                source="binance_futures_public",
                captured_at=received,
                requested_count=1,
                candles=(candle,),
            )
        )
        return True

    async def start(self, symbol):
        self.select(symbol)
        if self._task is None:
            self._task = asyncio.create_task(self._read(), name="native-futures-klines")
            self.ensure_prime()

    def ensure_prime(self):
        if (
            self.public is not None
            and self.clock.monotonic() >= self._next_prime
            and (self._prime_task is None or self._prime_task.done())
        ):
            self._prime_task = asyncio.create_task(self.prime(), name="native-kline-history")

    async def _read(self):
        while True:
            try:
                async with self.connect(
                    "wss://fstream.binance.com/market/ws",
                    proxy=self.proxy,
                    open_timeout=5,
                    close_timeout=1,
                    max_size=65536,
                    ping_interval=20,
                ) as socket:
                    await socket.send(
                        json.dumps({"method": "SUBSCRIBE", "params": self.subscriptions, "id": 1})
                    )
                    self.connected = True
                    async for raw in socket:
                        try:
                            if self.accept(raw):
                                self.failure = None
                        except (ValueError, TypeError, KeyError):
                            self.rejected_frames += 1
                            # An unrelated/stale frame cannot erase already validated bars.
                            # A missing second is detected by KlineBuffer.accept, and an
                            # actual disconnect still resets seconds in the finally block.
                            self.failure = "invalid_kline_frame"
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failure = "kline_stream_disconnected"
            finally:
                self.connected = False
                self.buffer.disconnect()
                self._next_prime = 0
            await asyncio.sleep(2)
            self.ensure_prime()

    async def aclose(self):
        tasks = tuple(t for t in (self._task, self._prime_task) if t is not None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._prime_task = None
        self.connected = False
        if self.buffer:
            self.buffer.disconnect()
