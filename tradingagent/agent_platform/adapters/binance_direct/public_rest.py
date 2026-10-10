"""Unauthenticated Spot public GETs on the market-data-only host.

No credentials, arbitrary URLs, redirects or automatic HTTP retries. Network
access happens only when the caller explicitly awaits a request.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from math import ceil

import httpx

from agent_platform.adapters.binance_direct.normalizer import (
    NormalizationError,
    normalize_kline_row,
)
from agent_platform.domain.market import MarketEvent
from agent_platform.ports.clock import ClockPort

PUBLIC_HOST = "https://data-api.binance.vision"
MAX_RESPONSE_BYTES = 262144
_PATHS = {
    "/api/v3/time": frozenset(),
    "/api/v3/klines": frozenset({"symbol", "interval", "limit", "startTime", "endTime"}),
    "/api/v3/exchangeInfo": frozenset({"symbol"}),
    "/api/v3/ticker/bookTicker": frozenset({"symbol"}),
}


class PublicRestError(RuntimeError):
    """Sanitized transport/protocol failure; raw body stays inside this adapter."""


class PublicRateLimited(PublicRestError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"public market requests paused for {retry_after_seconds} seconds")


def _params(path: str, params: dict | None) -> dict:
    if path not in _PATHS or (params is not None and type(params) is not dict):
        raise ValueError("unsupported public request")
    result = dict(params or {})
    if set(result) - _PATHS[path]:
        raise ValueError("unsupported public query parameters")
    if path != "/api/v3/time" and result.get("symbol") != "BTCUSDT":
        raise ValueError("public query requires BTCUSDT")
    if path == "/api/v3/klines":
        if result.get("interval") != "1m":
            raise ValueError("only UTC minute klines are supported")
        limit = result.get("limit", 120)
        if type(limit) is not int or not 1 <= limit <= 120:
            raise ValueError("public kline limit must be 1..120")
        result["limit"] = limit
        for key in ("startTime", "endTime"):
            if key in result and (type(result[key]) is not int or result[key] < 0):
                raise ValueError("kline timestamps must be integer milliseconds")
        if result.get("startTime", 0) > result.get("endTime", 2**63 - 1):
            raise ValueError("kline range is reversed")
    return result


def _retry_delay(response: httpx.Response) -> int:
    header = response.headers.get("retry-after", "")
    if header.isascii() and header.isdigit() and len(header) <= 9 and int(header) > 0:
        return int(header)
    return 180 if response.status_code == 418 else 60


class PublicRestClient:
    def __init__(self, clock: ClockPort, *, transport: httpx.AsyncBaseTransport | None = None):
        self.clock = clock
        self._blocked_until = 0.0
        self._server_anchor: tuple[datetime, float] | None = None
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            base_url=PUBLIC_HOST,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx.Timeout(10),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> dict | list:
        query = _params(path, params)
        async with self._lock:
            remaining = self._blocked_until - self.clock.monotonic()
            if remaining > 0:
                raise PublicRateLimited(ceil(remaining))
            try:
                async with asyncio.timeout(15):
                    async with self._client.stream("GET", path, params=query) as response:
                        if response.status_code in (429, 418):
                            delay = _retry_delay(response)
                            self._blocked_until = self.clock.monotonic() + delay
                            raise PublicRateLimited(delay)
                        if response.status_code != 200:
                            raise PublicRestError(
                                f"public market HTTP status {response.status_code}"
                            )
                        if (
                            response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            raise PublicRestError("encoded public market response is unsupported")
                        content = bytearray()
                        if response.is_stream_consumed:
                            # Preloaded MockTransport responses have no raw iterator.
                            if len(response.content) > MAX_RESPONSE_BYTES:
                                raise PublicRestError("public market response exceeds byte limit")
                            content.extend(response.content)
                        else:
                            async for chunk in response.aiter_raw():
                                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                                    raise PublicRestError(
                                        "public market response exceeds byte limit"
                                    )
                                content.extend(chunk)
                        data = json.loads(content)
                        if type(data) not in (dict, list):
                            raise PublicRestError("invalid public market JSON shape")
                        return data
            except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, RecursionError):
                raise PublicRestError("public market transport/JSON failure") from None

    async def server_time(self) -> datetime:
        data = await self.get("/api/v3/time")
        try:
            timestamp = data["serverTime"]
            if type(timestamp) is not int or timestamp < 0:
                raise ValueError("invalid server time")
            result = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=timestamp)
            self._server_anchor = result, self.clock.monotonic()
            return result
        except (TypeError, KeyError, ValueError, OverflowError):
            raise PublicRestError("invalid public server time") from None

    async def completed_before(self) -> datetime:
        # Anchor at response arrival: extrapolation deliberately omits network
        # transit time, providing a lower bound rather than a midpoint estimate.
        if self._server_anchor is None or self.clock.monotonic() - self._server_anchor[1] >= 30:
            await self.server_time()
        server, observed = self._server_anchor
        elapsed = self.clock.monotonic() - observed
        if elapsed < 0:
            raise PublicRestError("public monotonic clock moved backwards")
        return min(self.clock.utcnow(), server + timedelta(seconds=elapsed))

    async def klines(
        self,
        symbol: str = "BTCUSDT",
        *,
        limit: int = 120,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> tuple[MarketEvent, ...]:
        params = {"symbol": symbol, "interval": "1m", "limit": limit}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        # Validate before even the prerequisite public time query.
        _params("/api/v3/klines", params)
        started = await self.completed_before()
        data = await self.get("/api/v3/klines", params)
        if type(data) is not list or len(data) > limit:
            raise PublicRestError("invalid public minute batch")
        received = self.clock.utcnow()
        try:
            events = tuple(
                normalize_kline_row(row, symbol, received, completed_before=started) for row in data
            )
            opens = tuple(item.payload.opened_at for item in events)
            if tuple(sorted(set(opens))) != opens:
                raise PublicRestError("public minute batch is unordered or duplicated")
            return events
        except NormalizationError:
            raise PublicRestError("invalid public minute payload") from None
