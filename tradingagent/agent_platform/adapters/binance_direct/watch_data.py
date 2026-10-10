"""Bounded REST warmup/recovery and closed USD-M perpetual kline subscriptions.

Protocol: developers.binance.com, USD-M Futures Market Streams / Kline.
Construct with a dedicated FuturesPublicClient to isolate JEV concurrency.
"""

import asyncio
import json
from datetime import datetime

from agent_platform.domain.market import Candle
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.watches import WATCH_LOOKBACK, WatchFrame, interval_delta

from .futures_public import _millis
from .futures_stream import _FuturesConnect


class BinanceWatchData:
    def __init__(self, clock, *, public, proxy_url=None, connect=_FuturesConnect):
        self.clock, self.public, self.proxy, self.connect = clock, public, proxy_url, connect

    @staticmethod
    def _scope(symbol, interval):
        SessionAnalysisTarget(market="usdt_perpetual", symbol=symbol)
        interval_delta(interval)

    def _clock_ready(self):
        calibration = getattr(self.clock, "public_status", None)
        if calibration and not calibration["ready"]:
            raise ValueError("watch clock is not synchronized")

    def decode(self, raw, symbol, interval):
        self._scope(symbol, interval)
        self._clock_ready()
        if type(raw) is not str or len(raw.encode()) > 65536:
            raise ValueError("invalid watch websocket frame")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate watch JSON key")
                result[key] = value
            return result

        value = json.loads(raw, object_pairs_hook=pairs)
        if type(value) is not dict:
            raise ValueError("invalid watch event")
        if value.get("result", "missing") is None and value.get("id") == 1:
            return None
        bar = value.get("k")
        if (
            value.get("e") != "kline"
            or value.get("s") != symbol
            or type(value.get("st", 1)) is not int
            or value.get("st", 1) != 1
            or type(bar) is not dict
            or bar.get("s") != symbol
            or bar.get("i") != interval
            or type(bar.get("x")) is not bool
        ):
            raise ValueError("wrong watch market, symbol or interval")
        received, occurred = self.clock.utcnow(), _millis(value["E"])
        if not 0 <= (received - occurred).total_seconds() <= 5:
            raise ValueError("future or stale watch event")
        if not bar["x"]:
            return None
        candle = Candle(
            symbol=symbol,
            opened_at=_millis(bar["t"]),
            closed_at=_millis(bar["T"]),
            open=bar["o"],
            high=bar["h"],
            low=bar["l"],
            close=bar["c"],
            volume=bar["v"],
            quote_volume=bar.get("q"),
        )
        self._frame((candle,), interval)
        return candle

    def _frame(self, bars, interval, quality="ready"):
        return WatchFrame.from_candles(
            candles=tuple(bars),
            interval=interval,
            source="binance_futures_public",
            data_version="watch-data-v1",
            received_at=self.clock.utcnow(),
            quality=quality,
        )

    async def _history(self, symbol, interval, count=WATCH_LOOKBACK):
        self._scope(symbol, interval)
        self._clock_ready()
        if not any(c.symbol == symbol for c in await self.public.catalog()):
            raise ValueError("watch requires an active USDT perpetual contract")
        now = self.clock.utcnow()
        step = interval_delta(interval)
        boundary = now.replace(second=0, microsecond=0)
        boundary = boundary.replace(
            minute=boundary.minute - boundary.minute % int(step.total_seconds() / 60)
        )
        rows = await self.public.get(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "limit": count,
                "endTime": int(boundary.timestamp() * 1000) - 1,
            },
        )
        if type(rows) is not list or not 1 <= len(rows) <= count:
            raise ValueError("invalid watch history size")
        bars = []
        for row in rows:
            if type(row) is not list or len(row) != 12:
                raise ValueError("invalid watch history bar")
            bars.append(
                Candle(
                    symbol=symbol,
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
        self._frame(bars, interval)
        if bars[-1].opened_at + step != boundary:
            raise ValueError("watch history does not reach the latest closed UTC boundary")
        return bars

    async def latest(self, symbol, interval):
        return self._frame(await self._history(symbol, interval), interval)

    async def stream(self, symbol, interval, after=None):
        self._scope(symbol, interval)
        cursor = None
        if after is not None:
            prefix = f"usdt_perpetual:{symbol}:{interval}:"
            if not after.startswith(prefix):
                raise ValueError("watch cursor belongs to another scope")
            cursor = datetime.fromisoformat(after[len(prefix) :])
            if cursor.tzinfo is None or cursor.utcoffset().total_seconds() != 0:
                raise ValueError("watch cursor must be UTC")
        bars = []
        while True:
            # REST reconnect repair is bounded to 120 bars. It restores the
            # contiguous feature window, and never invents missing candles.
            history = await self._history(symbol, interval)
            for candle in history:
                existing = next((b for b in bars if b.opened_at == candle.opened_at), None)
                if existing is not None and existing != candle:
                    yield self._frame([candle], interval, "data_conflict")
                    return
            previous = cursor
            if cursor is None:
                yield self._frame(history[-WATCH_LOOKBACK:], interval)
            else:
                for index, candle in enumerate(history):
                    close = candle.opened_at + interval_delta(interval)
                    if close >= cursor:
                        # If the downtime exceeds the bounded repair, block
                        # until a complete known indicator window is present.
                        quality = (
                            "ready"
                            if (
                                index >= 25
                                or close == cursor
                                or previous + interval_delta(interval) == close
                            )
                            else "data_gap"
                        )
                        yield self._frame(
                            history[max(0, index - WATCH_LOOKBACK + 1) : index + 1],
                            interval,
                            quality,
                        )
                        previous = close
            bars = history[-WATCH_LOOKBACK:]
            cursor = bars[-1].opened_at + interval_delta(interval)
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
                        json.dumps(
                            {
                                "method": "SUBSCRIBE",
                                "params": [f"{symbol.lower()}@kline_{interval}"],
                                "id": 1,
                            }
                        )
                    )
                    async for raw in socket:
                        candle = self.decode(raw, symbol, interval)
                        if candle is None:
                            continue
                        existing = next((b for b in bars if b.opened_at == candle.opened_at), None)
                        if existing is not None:
                            if existing != candle:
                                yield self._frame([candle], interval, "data_conflict")
                                return
                            continue
                        close = candle.opened_at + interval_delta(interval)
                        if close <= cursor:
                            continue
                        if close != cursor + interval_delta(interval):
                            # Close socket and repair before evaluating this bar.
                            break
                        bars = [*bars, candle][-WATCH_LOOKBACK:]
                        cursor = close
                        yield self._frame(bars, interval)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError):
                pass
            await asyncio.sleep(2)
