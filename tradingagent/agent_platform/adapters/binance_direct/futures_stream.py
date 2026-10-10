"""Selected USDT perpetual top of book and 1s marks; no exchange write port."""

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import quote as urlquote
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidHandshake

from agent_platform.domain.futures_market import FuturesClockEvidence, FuturesMarketSnapshot
from agent_platform.domain.futures_values import (
    FUTURES_CLOCK_UNCERTAINTY_MS,
    FuturesQuote,
    bounded_decimal,
)
from agent_platform.ports.futures_market import FuturesMarketUnavailable

from .futures_market import FuturesMarketClient, _number, _symbol
from .futures_public import _millis


class _ProxySafeConnection(ClientConnection):
    def connection_lost(self, exc):
        # websockets 16 HTTP CONNECT calls start_tls before connection_made.
        # A TLS reset then reaches this callback before its assembler exists.
        if not hasattr(self, "recv_messages"):
            self.protocol.receive_eof()
            self.set_recv_exc(exc)
            if not self.connection_lost_waiter.done():
                self.connection_lost_waiter.set_result(None)
            return
        super().connection_lost(exc)


class _FuturesConnect(websocket_connect):
    def __init__(self, uri, **options):
        target = urlsplit(uri)
        if (
            target.scheme != "wss"
            or target.netloc != "fstream.binance.com"
            or not (
                target.path == "/market/ws"
                or target.path.startswith(("/public/ws/", "/market/ws/"))
            )
            or target.query
            or target.fragment
        ):
            raise ValueError("unsupported futures stream URI")
        options.setdefault("create_connection", _ProxySafeConnection)
        super().__init__(uri, **options)

    def process_redirect(self, error):
        return InvalidHandshake("futures stream redirects are disabled")


class FuturesStreamMarket(FuturesMarketClient):
    def __init__(self, clock, *, transport=None, proxy_url=None, connect=_FuturesConnect):
        super().__init__(clock, transport=transport, proxy_url=proxy_url)
        self.proxy_url, self.connect = proxy_url, connect
        self._symbol = None
        self._tasks = ()
        self._select_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._connected = {"book": False, "mark": False}
        self._facts = {}
        self._recent = deque(maxlen=30)
        self._sampled_mark = None
        self._generation = 0
        self.last_failure = None
        self.last_transport_error = None
        self.rejected_frames = 0
        self.event_ahead_ms = None

    @property
    def public_status(self):
        calibration = getattr(self.clock, "public_status", None)
        return {
            "transport": "websocket",
            "connected": dict(self._connected),
            "failure": "futures_stream_clock_unsynchronized"
            if calibration and not calibration["ready"]
            else self.last_failure,
            "transport_error": self.last_transport_error,
            "rejected_frames": self.rejected_frames,
            "event_ahead_ms": self.event_ahead_ms,
            "clock_uncertainty_ms": FUTURES_CLOCK_UNCERTAINTY_MS,
            "recent_quote_count": len(self._recent),
            "clock": calibration,
        }

    def _clock_evidence(self):
        status = getattr(self.clock, "public_status", None)
        if status is None:
            return None
        if not status["ready"]:
            raise ValueError("futures_stream_clock_unsynchronized")
        return FuturesClockEvidence(
            source=status["source"],
            synchronized_at=datetime.fromisoformat(status["synchronized_at"]),
            uncertainty_ms=status["uncertainty_ms"],
            local_offset_ms=status["offset_ms"],
            age_seconds=status["age_seconds"],
        )

    def _invalidate(self):
        self._facts.clear()
        self._recent.clear()
        self._sampled_mark = None
        self._changed.set()

    async def aclose(self):
        self._generation += 1
        tasks, self._tasks = self._tasks, ()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connected = {"book": False, "mark": False}
        self._invalidate()
        await super().aclose()

    async def _select(self, symbol):
        async with self._select_lock:
            if self._symbol == symbol and self._tasks:
                return
            self._generation += 1
            old = self._tasks
            for task in old:
                task.cancel()
            if old:
                await asyncio.gather(*old, return_exceptions=True)
            self._symbol = symbol
            self._connected = {"book": False, "mark": False}
            self._invalidate()
            stream = urlquote(symbol.lower(), safe="")
            self._tasks = tuple(
                asyncio.create_task(
                    self._read(kind, uri, self._generation), name="futures-stream-" + kind
                )
                for kind, uri in (
                    ("book", f"wss://fstream.binance.com/public/ws/{stream}@bookTicker"),
                    ("mark", f"wss://fstream.binance.com/market/ws/{stream}@markPrice@1s"),
                )
            )

    async def _read(self, kind, uri, generation):
        while generation == self._generation:
            try:
                async with self.connect(
                    uri,
                    proxy=self.proxy_url,
                    open_timeout=5,
                    close_timeout=1,
                    max_size=65536,
                    max_queue=32,
                    ping_interval=20,
                    ping_timeout=20,
                ) as socket:
                    self._connected[kind] = True
                    async for raw in socket:
                        if generation != self._generation:
                            return
                        try:
                            self.accept(kind, raw, self.clock.utcnow())
                        except (ValueError, TypeError, KeyError, OverflowError) as error:
                            self.rejected_frames += 1
                            self.last_failure = (
                                str(error)
                                if str(error)
                                in {"futures_stream_future", "futures_stream_clock_unsynchronized"}
                                else "futures_stream_invalid"
                            )
                            self._invalidate()
                    raise ConnectionError("futures stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if generation != self._generation:
                    return
                self.last_failure = "futures_stream_disconnected"
                self.last_transport_error = type(error).__name__
                self._connected[kind] = False
                self._invalidate()
                await asyncio.sleep(1)

    def accept(self, kind, raw, received):
        clock_evidence = self._clock_evidence()
        if type(raw) not in (str, bytes) or len(raw) > 65536:
            raise ValueError("invalid stream frame")
        data = json.loads(raw)
        if (
            type(data) is not dict
            or data.get("s") != self._symbol
            or data.get("e") != ("bookTicker" if kind == "book" else "markPriceUpdate")
            or ("st" in data and (type(data["st"]) is not int or data["st"] != 1))
        ):
            raise ValueError("wrong futures stream identity")
        at = _millis(data["E"])
        uncertainty = timedelta(milliseconds=FUTURES_CLOCK_UNCERTAINTY_MS)
        if at - received > uncertainty:
            self.event_ahead_ms = round((at - received).total_seconds() * 1000, 1)
            raise ValueError("futures_stream_future")
        if not -uncertainty <= received - at <= timedelta(seconds=5):
            raise ValueError("future or stale at first reception")
        old = self._facts.get(kind)
        if kind == "book":
            update_id = data["u"]
            if type(update_id) is not int or update_id < 0:
                raise ValueError("invalid book update sequence")
            if old is not None and (at < old["at"] or update_id <= old["update_id"]):
                return False
            transaction = _millis(data["T"])
            if transaction > at or received - transaction > timedelta(seconds=5):
                raise ValueError("invalid book transaction time")
            values = {
                "bid": _number(data["b"]),
                "ask": _number(data["a"]),
                "bid_quantity": _number(data["B"]),
                "ask_quantity": _number(data["A"]),
                "update_id": update_id,
            }
        else:
            if old is not None and at <= old["at"]:
                return False
            values = {
                "mark": _number(data["p"]),
                "index_price": _number(data["i"]),
                "displayed_funding_rate": _number(data["r"]),
                "next_funding_at": _millis(data["T"]),
            }
        self._facts[kind] = {
            "at": at,
            "received": received,
            "clock_evidence": clock_evidence,
            **values,
        }
        if len(self._facts) == 2 and all(self._connected.values()):
            mark_at = self._facts["mark"]["at"]
            if self._sampled_mark != mark_at:
                snapshot = self.current_snapshot(self._symbol)
                self._recent.append(snapshot.quote)
                self._sampled_mark = mark_at
            else:
                # Validate current prices on each book event, but the complete
                # recent history is only built for a new mark or an actual read.
                self._current_quote(self._symbol)
            self.last_failure = None
            self.event_ahead_ms = None
        self._changed.set()
        return True

    def _current_quote(self, symbol):
        try:
            self._clock_evidence()  # Expired calibration invalidates cached quotes as well.
            if symbol != self._symbol or not all(self._connected.values()):
                raise ValueError("streams not ready")
            book, mark = self._facts["book"], self._facts["mark"]
            now = self.clock.utcnow()
            if (
                min(book["received"], mark["received"]) > now
                or max(book["received"], mark["received"]) > now
                or now - min(book["at"], mark["at"]) > timedelta(seconds=5)
            ):
                raise ValueError("stream quote no longer fresh")
            if any(bounded_decimal(book[key]) <= 0 for key in ("bid_quantity", "ask_quantity")):
                raise ValueError("invalid book quantity")
            return FuturesQuote(
                symbol=symbol,
                source="binance_futures_public",
                bid=book["bid"],
                ask=book["ask"],
                mark=mark["mark"],
                book_at=book["at"],
                mark_at=mark["at"],
                received_at=max(book["received"], mark["received"]),
            )
        except (ValueError, TypeError, KeyError):
            raise FuturesMarketUnavailable("futures_stream_quote_unavailable") from None

    def current_snapshot(self, symbol):
        try:
            quote = self._current_quote(symbol)
            book, mark = self._facts["book"], self._facts["mark"]
            return FuturesMarketSnapshot(
                quote=quote,
                index_price=mark["index_price"],
                displayed_funding_rate=mark["displayed_funding_rate"],
                next_funding_at=mark["next_funding_at"],
                bid_quantity=book["bid_quantity"],
                ask_quantity=book["ask_quantity"],
                recent_quotes=tuple(self._recent),
                clock_evidence=max((book, mark), key=lambda fact: fact["received"])[
                    "clock_evidence"
                ],
            )
        except (ValueError, TypeError, KeyError):
            raise FuturesMarketUnavailable("futures_stream_quote_unavailable") from None

    async def snapshot(self, symbol):
        symbol = _symbol(symbol)
        await self._select(symbol)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while True:
            self._changed.clear()
            try:
                return self.current_snapshot(symbol)
            except FuturesMarketUnavailable:
                left = deadline - loop.time()
                if left <= 0:
                    raise
                try:
                    await asyncio.wait_for(self._changed.wait(), left)
                except TimeoutError:
                    raise FuturesMarketUnavailable("futures_stream_quote_unavailable") from None
