"""Fixed-host GET-only account transport. Auth failure cannot enable execution."""

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from math import ceil

import httpx

from agent_platform.adapters.binance_direct.public_rest import _params as public_params
from agent_platform.adapters.binance_direct.public_rest import _retry_delay
from agent_platform.adapters.binance_direct.signing import (
    HmacCredentials,
    sign_query,
    unix_milliseconds,
)
from agent_platform.ports.clock import ClockPort

READ_HOST = "https://api.binance.com"
_PRIVATE = {
    "/api/v3/account": frozenset(),
    "/api/v3/openOrders": frozenset({"symbol"}),
    "/api/v3/order": frozenset({"symbol", "orderId", "origClientOrderId"}),
    "/api/v3/myTrades": frozenset({"symbol", "fromId", "orderId", "limit", "startTime", "endTime"}),
}
_LOG_SECRETS: ContextVar[tuple[str, ...]] = ContextVar("btc_signed_log_secrets", default=())


class ReadOnlyError(RuntimeError):
    pass


class CredentialsUnavailable(ReadOnlyError):
    pass


class AuthenticationRejected(ReadOnlyError):
    pass


class ReadRateLimited(ReadOnlyError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Binance reads paused for {retry_after_seconds} seconds")


class _SignedLogFilter(logging.Filter):
    """Redact SDK logging only within this task's signed-request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        sensitive = _LOG_SECRETS.get()
        if sensitive:
            message = record.getMessage()
            for value in sensitive:
                escaped = value.replace("\\", "\\\\")
                variants = {
                    value,
                    repr(value)[1:-1],
                    escaped.replace("'", "\\'"),
                    escaped.replace('"', '\\"'),
                    escaped.replace("'", "\\'").replace('"', '\\"'),
                }
                for variant in sorted(variants, key=len, reverse=True):
                    message = message.replace(variant, "[REDACTED]")
            record.msg, record.args = message, ()
        return True


def _log_filters() -> None:
    for name in (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _SignedLogFilter) for item in logger.filters):
            logger.addFilter(_SignedLogFilter())


def _params(method: str, path: str, params: dict | None) -> tuple[dict, bool]:
    if (
        method != "GET"
        or type(path) is not str
        or (params is not None and type(params) is not dict)
    ):
        raise ValueError("unsupported read-only request")
    if path not in _PRIVATE:
        return public_params(path, params), False
    result = dict(params or {})
    if set(result) - _PRIVATE[path]:
        raise ValueError("unsupported account query parameters")
    if path != "/api/v3/account" and result.get("symbol") != "BTCUSDT":
        raise ValueError("account query requires BTCUSDT")
    for name in ("orderId", "fromId", "startTime", "endTime", "limit"):
        if name in result and (type(result[name]) is not int or not 0 <= result[name] <= 2**63 - 1):
            raise ValueError("account query requires bounded integer values")
    if "origClientOrderId" in result and (
        type(result["origClientOrderId"]) is not str
        or re.fullmatch(r"[A-Za-z0-9._:/-]{1,36}", result["origClientOrderId"]) is None
    ):
        raise ValueError("invalid client order identity")
    if path == "/api/v3/order" and ("orderId" in result) == ("origClientOrderId" in result):
        raise ValueError("query order requires exactly one order identity")
    if path == "/api/v3/myTrades":
        filters = frozenset(result) - {"symbol", "limit"}
        allowed = (
            frozenset(),
            frozenset({"orderId"}),
            frozenset({"startTime"}),
            frozenset({"endTime"}),
            frozenset({"fromId"}),
            frozenset({"startTime", "endTime"}),
            frozenset({"orderId", "fromId"}),
        )
        if filters not in allowed:
            raise ValueError("unsupported trade filter combination")
        result.setdefault("limit", 1000)
        if not 1 <= result["limit"] <= 1000:
            raise ValueError("trade page size must be 1..1000")
        if "startTime" in result and "endTime" in result:
            span = result["endTime"] - result["startTime"]
            if not 0 <= span <= 86400000:
                raise ValueError("trade time range must be at most 24 hours")
    return result, True


async def _json(response: httpx.Response) -> dict | list:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ReadOnlyError("encoded account response is unsupported")
    maximum = 1048576
    content = bytearray()
    if response.is_stream_consumed:
        if len(response.content) > maximum:
            raise ReadOnlyError("account response exceeds byte limit")
        content.extend(response.content)
    else:
        async for chunk in response.aiter_raw():
            if len(content) + len(chunk) > maximum:
                raise ReadOnlyError("account response exceeds byte limit")
            content.extend(chunk)
    result = json.loads(content)
    if type(result) not in (dict, list):
        raise ReadOnlyError("invalid account JSON shape")
    return result


class ReadOnlyClient:
    def __init__(
        self,
        clock: ClockPort,
        credentials: HmacCredentials | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if credentials is not None and not isinstance(credentials, HmacCredentials):
            raise ValueError("unsupported read-only credentials")
        self.clock, self._credentials = clock, credentials
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self._auth_rejected = False
        self._anchor: tuple[datetime, float] | None = None
        self._client = httpx.AsyncClient(
            base_url=READ_HOST,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx.Timeout(10),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
        _log_filters()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _pause(self) -> None:
        remaining = self._blocked_until - self.clock.monotonic()
        if remaining > 0:
            raise ReadRateLimited(ceil(remaining))

    def _set_anchor(self, data: dict | list) -> None:
        try:
            milliseconds = data["serverTime"]
            if type(milliseconds) is not int or milliseconds < 0:
                raise ValueError("invalid server time")
            server = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
            self._anchor = server, self.clock.monotonic()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ReadOnlyError("invalid read-only server clock") from None

    async def _fetch(self, path: str, params: dict, *, signed: bool) -> dict | list:
        self._pause()
        headers, token = {}, None
        if signed:
            packet = sign_query(params, self._credentials.secret_key)
            path += "?" + packet.query
            headers["X-MBX-APIKEY"] = self._credentials.api_key.get_secret_value()
            token = _LOG_SECRETS.set(
                (
                    headers["X-MBX-APIKEY"],
                    self._credentials.secret_key.get_secret_value(),
                    packet.query.rsplit("&signature=", 1)[1],
                )
            )
        try:
            async with asyncio.timeout(15):
                async with self._client.stream(
                    "GET", path, params=None if signed else params, headers=headers
                ) as response:
                    if response.status_code in (429, 418):
                        delay = _retry_delay(response)
                        self._blocked_until = self.clock.monotonic() + delay
                        raise ReadRateLimited(delay)
                    if signed and response.status_code in (401, 403):
                        self._auth_rejected = True
                        raise AuthenticationRejected("Binance read-only authentication unavailable")
                    if 300 <= response.status_code < 400:
                        raise ReadOnlyError("read-only redirects are disabled")
                    data = await _json(response)
                    code = data.get("code") if isinstance(data, dict) else None
                    if signed and code in (-2014, -2015, -1022):
                        self._auth_rejected = True
                        raise AuthenticationRejected("Binance read-only authentication unavailable")
                    if code == -1021:
                        self._anchor = None
                    if response.status_code != 200 or (type(code) is int and code < 0):
                        raise ReadOnlyError(f"Binance read-only HTTP status {response.status_code}")
                    return data
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, RecursionError):
            raise ReadOnlyError("Binance read-only transport/JSON failure") from None
        finally:
            if token is not None:
                _LOG_SECRETS.reset(token)

    async def request(self, method: str, path: str, params: dict | None = None) -> dict | list:
        query, signed = _params(method, path, params)
        async with self._lock:
            self._pause()
            if signed:
                if self._credentials is None:
                    raise CredentialsUnavailable("Binance read-only credentials are unavailable")
                if self._auth_rejected:
                    raise AuthenticationRejected("Binance read-only authentication unavailable")
                if self._anchor is None or self.clock.monotonic() - self._anchor[1] >= 30:
                    self._set_anchor(await self._fetch("/api/v3/time", {}, signed=False))
                server, sampled = self._anchor
                elapsed = self.clock.monotonic() - sampled
                if elapsed < 0:
                    raise ReadOnlyError("read-only monotonic clock moved backwards")
                query["timestamp"] = unix_milliseconds(server + timedelta(seconds=elapsed))
                query["recvWindow"] = 5000
            data = await self._fetch(path, query, signed=signed)
            if path == "/api/v3/time":
                self._set_anchor(data)
            return data
