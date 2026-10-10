"""USDT perpetual public GETs. No account credentials, orders or arbitrary URLs."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from math import ceil

import httpx

from agent_platform.domain.futures_chart import (
    NATIVE_INTERVALS,
    ChartHistory,
    ChartRequest,
    shift_open,
)
from agent_platform.domain.market import Candle
from agent_platform.domain.session_market import (
    HistoricalMarketData,
    PerpetualContract,
    SessionAnalysisTarget,
)
from agent_platform.ports.session_analysis import HistoricalDataUnavailable

HOST = "https://fapi.binance.com"
MAX_BYTES = 4 * 1024 * 1024
PATHS = {
    "/fapi/v1/time": frozenset(),
    "/fapi/v1/exchangeInfo": frozenset(),
    "/fapi/v1/klines": frozenset({"symbol", "interval", "startTime", "endTime", "limit"}),
    "/fapi/v1/premiumIndex": frozenset({"symbol"}),
    "/fapi/v1/ticker/bookTicker": frozenset({"symbol"}),
    "/fapi/v1/fundingRate": frozenset({"symbol", "startTime", "endTime", "limit"}),
}


class FuturesPublicError(HistoricalDataUnavailable):
    pass


def _millis(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("invalid exchange timestamp")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


class FuturesPublicClient:
    def __init__(self, clock, *, transport=None, proxy_url=None):
        if proxy_url is not None:
            from agent_platform.config import RuntimeConfig

            RuntimeConfig(futures_proxy=proxy_url)
        self.clock = clock
        self._transport, self._proxy_url = transport, proxy_url
        self._client = self._new_client()
        self._lock = asyncio.Lock()
        self._catalog = None
        self._catalog_at = 0.0
        self._blocked_until = 0.0
        self._chart_lock = asyncio.Lock()
        self._chart_cache = {}

    def _new_client(self):
        return httpx.AsyncClient(
            base_url=HOST,
            proxy=self._proxy_url,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=10,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    @asynccontextmanager
    async def _public_stream(self, path, query):
        yielded = False
        for attempt in range(2):
            try:
                async with self._client.stream("GET", path, params=query) as response:
                    yielded = True
                    yield response
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                # Only before a response; never replay a partially read body or rate limit.
                if yielded:
                    raise
                # Failed proxy TLS tunnels can remain ACTIVE in httpcore's pool.
                # get() serializes this client, so no other request is invalidated.
                await self._client.aclose()
                self._client = self._new_client()
                if attempt:
                    raise
                await asyncio.sleep(0.25)

    async def get(self, path, params=None):
        if path not in PATHS or (params is not None and type(params) is not dict):
            raise ValueError("unsupported futures public request")
        query = dict(params or {})
        if set(query) - PATHS[path]:
            raise ValueError("unsupported futures query")
        if path in ("/fapi/v1/premiumIndex", "/fapi/v1/ticker/bookTicker"):
            SessionAnalysisTarget(market="usdt_perpetual", symbol=query.get("symbol"))
        if path == "/fapi/v1/fundingRate":
            SessionAnalysisTarget(market="usdt_perpetual", symbol=query.get("symbol"))
            if type(query.get("limit")) is not int or not 1 <= query["limit"] <= 1000:
                raise ValueError("unsupported funding query")
            start, end = _millis(query.get("startTime")), _millis(query.get("endTime"))
            if start > end or end - start > timedelta(days=31):
                raise ValueError("unsupported funding range")
        if path.endswith("klines"):
            SessionAnalysisTarget(market="usdt_perpetual", symbol=query.get("symbol"))
            if (
                type(query.get("interval")) is not str
                or query["interval"] not in NATIVE_INTERVALS
                or type(query.get("limit")) is not int
                or not 1 <= query["limit"] <= 499
            ):
                raise ValueError("unsupported history query")
            start = _millis(query["startTime"]) if "startTime" in query else None
            end = _millis(query["endTime"]) if "endTime" in query else None
            if start is not None and end is not None and start > end:
                raise ValueError("reversed history query")
        async with self._lock:
            remaining = self._blocked_until - self.clock.monotonic()
            if remaining > 0:
                raise FuturesPublicError(f"rate_limited:{ceil(remaining)}")
            try:
                async with asyncio.timeout(15):
                    async with self._public_stream(path, query) as response:
                        if response.status_code in (418, 429):
                            raw = response.headers.get("retry-after", "")
                            delay = (
                                int(raw)
                                if raw.isascii() and raw.isdigit() and len(raw) <= 6
                                else 180
                            )
                            self._blocked_until = self.clock.monotonic() + max(60, delay)
                            raise FuturesPublicError("rate_limited")
                        if response.status_code != 200:
                            raise FuturesPublicError("futures_public_unavailable")
                        if (
                            response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            raise FuturesPublicError("invalid_futures_response")
                        raw = bytearray()
                        if response.is_stream_consumed:
                            if len(response.content) > MAX_BYTES:
                                raise FuturesPublicError("futures_response_too_large")
                            raw.extend(response.content)
                        else:
                            async for chunk in response.aiter_raw():
                                if len(raw) + len(chunk) > MAX_BYTES:
                                    raise FuturesPublicError("futures_response_too_large")
                                raw.extend(chunk)

                        def pairs(items):
                            result = {}
                            for key, value in items:
                                if key in result:
                                    raise ValueError("duplicate JSON key")
                                result[key] = value
                            return result

                        result = json.loads(raw, object_pairs_hook=pairs)
                        if type(result) not in (dict, list):
                            raise ValueError("response shape")
                        return result
            except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, RecursionError):
                raise FuturesPublicError("futures_transport_or_data_error") from None

    async def catalog(self):
        age = self.clock.monotonic() - self._catalog_at
        if self._catalog is not None and 0 <= age < 300:
            return self._catalog
        value = await self.get("/fapi/v1/exchangeInfo")
        try:
            rows = value["symbols"]
            if type(rows) is not list or len(rows) > 10000:
                raise ValueError("catalog shape")
            contracts = []
            for item in rows:
                if type(item) is not dict:
                    raise ValueError("contract shape")
                if (
                    item.get("contractType"),
                    item.get("status"),
                    item.get("quoteAsset"),
                    item.get("marginAsset"),
                ) != ("PERPETUAL", "TRADING", "USDT", "USDT"):
                    continue
                contracts.append(
                    PerpetualContract(symbol=item["symbol"], base_asset=item["baseAsset"])
                )
            if not contracts or len({c.symbol for c in contracts}) != len(contracts):
                raise ValueError("empty or duplicate catalog")
            self._catalog = tuple(sorted(contracts, key=lambda c: c.symbol))
            self._catalog_at = self.clock.monotonic()
            return self._catalog
        except (ValueError, TypeError, KeyError):
            raise FuturesPublicError("invalid_contract_catalog") from None

    async def history(self, target):
        target = SessionAnalysisTarget.model_validate_json(target.model_dump_json())
        contracts = await self.catalog()
        contract = next(
            (c for c in contracts if c.symbol == target.symbol and c.market == target.market), None
        )
        if contract is None:
            raise FuturesPublicError("contract_not_available")
        try:
            server = _millis((await self.get("/fapi/v1/time"))["serverTime"])
            captured = self.clock.utcnow()
            end = min(server, captured).replace(minute=0, second=0, microsecond=0)
            start = end - timedelta(days=target.history_days)
            candles = []
            remaining, cursor = target.history_days * 24, start
            while remaining:
                limit = min(remaining, 499)
                rows = await self.get(
                    "/fapi/v1/klines",
                    {
                        "symbol": target.symbol,
                        "interval": target.interval,
                        "limit": limit,
                        "startTime": int(cursor.timestamp() * 1000),
                        "endTime": int(end.timestamp() * 1000) - 1,
                    },
                )
                if type(rows) is not list or len(rows) != limit:
                    raise ValueError("incomplete history")
                for row in rows:
                    if type(row) is not list or len(row) != 12:
                        raise ValueError("invalid kline row")
                    candles.append(
                        Candle(
                            symbol=target.symbol,
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
                cursor += timedelta(hours=limit)
                remaining -= limit
            return HistoricalMarketData(
                target=target,
                contract=contract,
                source="binance_futures_public",
                captured_at=captured,
                requested_start=start,
                requested_end=end,
                candles=tuple(candles),
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            raise FuturesPublicError("history_incomplete_or_invalid") from None

    async def chart(self, request):
        request = ChartRequest.model_validate_json(request.model_dump_json())
        key = (request.symbol, request.interval, request.limit)
        async with self._chart_lock:
            cached = self._chart_cache.get(key)
            if cached is not None and 0 <= self.clock.monotonic() - cached[0] < 15:
                return cached[1]
            if not any(c.symbol == request.symbol for c in await self.catalog()):
                raise FuturesPublicError("contract_not_available")
            try:
                server = _millis((await self.get("/fapi/v1/time"))["serverTime"])
                captured = min(server, self.clock.utcnow())
                rows = await self.get(
                    "/fapi/v1/klines",
                    {
                        "symbol": request.symbol,
                        "interval": request.interval,
                        "limit": min(499, request.limit + 1),
                        "endTime": int(captured.timestamp() * 1000) - 1,
                    },
                )
                if type(rows) is not list or not 1 <= len(rows) <= min(499, request.limit + 1):
                    raise ValueError("invalid chart count")
                bars = []
                incomplete = 0
                previous_open = None
                for i, row in enumerate(rows):
                    if type(row) is not list or len(row) != 12:
                        raise ValueError("invalid chart row")
                    candle = Candle(
                        symbol=request.symbol,
                        opened_at=_millis(row[0]),
                        closed_at=_millis(row[6]),
                        open=row[1],
                        high=row[2],
                        low=row[3],
                        close=row[4],
                        volume=row[5],
                        quote_volume=row[7],
                    )
                    if (
                        candle.opened_at >= captured
                        or candle.closed_at
                        != shift_open(candle.opened_at, request.interval)
                        - timedelta(milliseconds=1)
                        or (
                            previous_open is not None
                            and candle.opened_at != shift_open(previous_open, request.interval)
                        )
                    ):
                        raise ValueError("invalid chart time")
                    previous_open = candle.opened_at
                    if candle.closed_at >= captured:
                        incomplete += 1
                        if i != len(rows) - 1 or incomplete > 1:
                            raise ValueError("unfinished chart bars must be last")
                    else:
                        bars.append(candle)
                history = ChartHistory(
                    symbol=request.symbol,
                    interval=request.interval,
                    source="binance_futures_public",
                    captured_at=captured,
                    candles=tuple(bars[-request.limit :]),
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                raise FuturesPublicError("history_incomplete_or_invalid") from None
            if key not in self._chart_cache and len(self._chart_cache) >= 16:
                self._chart_cache.pop(next(iter(self._chart_cache)))
            self._chart_cache[key] = (self.clock.monotonic(), history)
            return history
