"""Single-consumer public Spot stream with bounded bootstrap and gap recovery."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidHandshake, WebSocketException

from agent_platform.adapters.binance_direct.normalizer import (
    NormalizationError,
    normalize_stream,
)
from agent_platform.adapters.binance_direct.public_rest import PublicRateLimited, PublicRestError
from agent_platform.domain.market import MarketEvent, MarketSnapshot
from agent_platform.ports.clock import ClockPort
from agent_platform.runtime.buffer import MarketBuffer

STREAM_URL = (
    "wss://data-stream.binance.vision/stream?streams="
    "btcusdt@trade/btcusdt@bookTicker/btcusdt@kline_1m"
)


@dataclass(frozen=True)
class StreamStats:
    connections: int
    reconnects: int
    accepted: int
    duplicates: int
    failures: int
    overflows: int
    queue_high_water: int
    reconciliation_failures: int
    last_error: str | None


class _StreamInterrupted(RuntimeError):
    pass


class _PublicConnect(websocket_connect):
    """websockets follows redirects by default; this public transport never does."""

    def __init__(self, uri: str, **options):
        if uri != STREAM_URL:
            raise ValueError("unsupported public WebSocket URI")
        super().__init__(uri, **options)

    def process_redirect(self, error: Exception) -> Exception:
        return InvalidHandshake("public stream handshake failed; redirects are disabled")


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class BinanceMarketStream:
    def __init__(
        self,
        rest: Any,
        clock: ClockPort,
        *,
        buffer: MarketBuffer | None = None,
        connect: Callable[..., Any] = _PublicConnect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        queue_capacity: int = 4096,
    ):
        if type(queue_capacity) is not int or not 1 <= queue_capacity <= 4096:
            raise ValueError("stream queue capacity must be 1..4096")
        self.rest, self.clock = rest, clock
        self.buffer = buffer or MarketBuffer()
        if self.buffer.symbol != "BTCUSDT":
            raise ValueError("stream requires a BTCUSDT buffer")
        self._connect, self._sleep = connect, sleep
        self._capacity = queue_capacity
        self._running = False
        self._connections = self._reconnects = self._accepted = self._duplicates = 0
        self._failures = self._overflows = self._high_water = self._reconciliation_failures = 0
        self._last_error: str | None = None
        self._next_restore_at = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stats(self) -> StreamStats:
        return StreamStats(
            self._connections,
            self._reconnects,
            self._accepted,
            self._duplicates,
            self._failures,
            self._overflows,
            self._high_water,
            self._reconciliation_failures,
            self._last_error,
        )

    async def latest(self, symbol: str) -> MarketSnapshot:
        return self.buffer.snapshot(symbol, self.clock.utcnow())

    async def _restore(self) -> None:
        completed = (await self.rest.completed_before()).replace(second=0, microsecond=0)
        elapsed = completed - datetime(1970, 1, 1, tzinfo=UTC)
        end_ms = elapsed.days * 86400000 + elapsed.seconds * 1000 - 1
        events = await self.rest.klines(end_time_ms=end_ms)
        proven = await self.rest.completed_before()
        now = self.clock.utcnow()
        if proven.replace(second=0, microsecond=0) > completed:
            self._next_restore_at = now
        else:
            self._next_restore_at = (
                now
                + (proven.replace(second=0, microsecond=0) + timedelta(minutes=1) - proven)
                + timedelta(milliseconds=1)
            )
        try:
            self.buffer.reconcile((event for event in events if event.payload.is_closed), now)
        except ValueError:
            # Incomplete/mismatched evidence must keep GAP; live quotes can still be observed.
            self._reconciliation_failures += 1
            self._last_error = "minute_reconciliation_failed"
            self.buffer.mark_gap(
                self._last_error,
                now,
                missing_from=min(
                    (event.payload.opened_at for event in events if event.payload.is_closed),
                    default=now,
                ),
            )

    async def _read(self, socket: Any, queue: asyncio.Queue[MarketEvent], started_at) -> None:
        started_monotonic = self.clock.monotonic()
        try:
            while True:
                # Rotate ahead of Binance's 24-hour connection limit.
                remaining = 85800 - (self.clock.monotonic() - started_monotonic)
                if remaining <= 0:
                    raise _StreamInterrupted("connection_rotation")
                async with asyncio.timeout(min(30, remaining)):
                    raw = await socket.recv()
                received = self.clock.utcnow()
                if not isinstance(raw, (str, bytes)) or len(raw) > 65536:
                    raise _StreamInterrupted("invalid_stream_frame")
                if isinstance(raw, str) and len(raw.encode("utf-8")) > 65536:
                    raise _StreamInterrupted("invalid_stream_frame")
                data = json.loads(raw)
                if type(data) is not dict:
                    raise _StreamInterrupted("invalid_stream_frame")
                detail = data.get("data", data)
                if isinstance(detail, dict) and detail.get("e") == "serverShutdown":
                    raise _StreamInterrupted("server_shutdown")
                event = normalize_stream(data, received)
                # Receipt handling is independent of downstream consumption speed.
                # REST watermark advancement cannot strand already captured facts.
                result = self.buffer.append(event)
                self._duplicates += int(result.duplicate)
                if not result.accepted:
                    if result.reason == "pending_capacity_exceeded":
                        raise _StreamInterrupted("pending_capacity_exceeded")
                    continue
                self._accepted += 1
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._overflows += 1
                    raise _StreamInterrupted("stream_queue_overflow") from None
                self._high_water = max(self._high_water, queue.qsize())
        except (
            OSError,
            TimeoutError,
            WebSocketException,
            ValueError,
            UnicodeError,
            RecursionError,
            _StreamInterrupted,
        ) as error:
            reason = (
                str(error)
                if isinstance(error, _StreamInterrupted)
                else "stream_transport_or_protocol_failure"
            )
            self._last_error = reason
            self.buffer.mark_gap(reason, self.clock.utcnow(), missing_from=started_at)
            raise _StreamInterrupted(reason) from None

    async def _bootstrap(self, reader: asyncio.Task) -> None:
        restore = asyncio.create_task(self._restore(), name="btc-market-bootstrap")
        try:
            await asyncio.wait((reader, restore), return_when=asyncio.FIRST_COMPLETED)
            if reader.done():
                reader.result()
            await restore
        finally:
            await _cancel(restore)

    async def _next(self, reader: asyncio.Task, queue: asyncio.Queue) -> MarketEvent:
        if reader.done():
            reader.result()
        getter = asyncio.create_task(queue.get(), name="btc-market-next")
        try:
            await asyncio.wait((reader, getter), return_when=asyncio.FIRST_COMPLETED)
            if reader.done():
                reader.result()
            return getter.result()
        finally:
            await _cancel(getter)

    async def stream(self, symbols: tuple[str, ...]) -> AsyncIterator[MarketEvent]:
        if type(symbols) is not tuple or symbols != ("BTCUSDT",):
            raise ValueError("stream supports exactly BTCUSDT")
        if self._running:
            raise RuntimeError("market stream already has an active consumer")
        self._running = True
        attempt = 0
        try:
            while True:
                reader = None
                started = self.clock.utcnow()
                try:
                    async with self._connect(
                        STREAM_URL,
                        proxy=None,
                        compression=None,
                        ping_interval=None,
                        open_timeout=10,
                        close_timeout=5,
                        max_size=65536,
                        max_queue=32,
                    ) as socket:
                        self._connections += 1
                        queue = asyncio.Queue(maxsize=self._capacity)
                        reader = asyncio.create_task(
                            self._read(socket, queue, started), name="btc-market-reader"
                        )
                        await self._bootstrap(reader)
                        while True:
                            event = await self._next(reader, queue)
                            if self.clock.utcnow() >= self._next_restore_at:
                                await self._bootstrap(reader)
                            attempt = 0
                            yield event
                except (
                    OSError,
                    TimeoutError,
                    WebSocketException,
                    NormalizationError,
                    ValueError,
                    PublicRestError,
                    _StreamInterrupted,
                ) as error:
                    self._failures += 1
                    self._last_error = (
                        str(error)
                        if isinstance(error, _StreamInterrupted)
                        else "public_market_connection_failed"
                    )
                    self.buffer.mark_gap(
                        self._last_error, self.clock.utcnow(), missing_from=started
                    )
                    delay = min(60, 2 * 2 ** min(attempt, 5))
                    if isinstance(error, PublicRateLimited):
                        delay = max(delay, error.retry_after_seconds)
                    attempt += 1
                finally:
                    await _cancel(reader)
                self._reconnects += 1
                await self._sleep(delay)
        finally:
            self._running = False
            self.buffer.mark_gap("stream_stopped", self.clock.utcnow())
