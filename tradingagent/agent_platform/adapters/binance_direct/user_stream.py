"""Optional current USER_STREAM signature subscription, carrying reconciliation hints.

REST remains authoritative. This adapter does not expose a general WebSocket API
request function and never sends execution, transfer or withdrawal methods.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from math import ceil
from uuid import uuid4

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidHandshake, InvalidStatus, WebSocketException

from agent_platform.adapters.binance_direct.account import _port_errors
from agent_platform.adapters.binance_direct.public_rest import _retry_delay
from agent_platform.adapters.binance_direct.read_client import ReadOnlyClient
from agent_platform.adapters.binance_direct.signing import HmacCredentials, hmac_signature
from agent_platform.domain.common import live_account_ref
from agent_platform.domain.sync import AccountSignal
from agent_platform.ports.account import AccountReadUnavailable
from agent_platform.ports.clock import ClockPort

USER_STREAM_URL = "wss://ws-api.binance.com:443/ws-api/v3"
_REQUEST_ID = "read-only-subscription"


class _UserConnect(websocket_connect):
    def __init__(self, uri: str, **options):
        if uri != USER_STREAM_URL:
            raise ValueError("unsupported private WebSocket URI")
        super().__init__(uri, **options)

    def process_redirect(self, error: Exception) -> Exception:
        if isinstance(error, InvalidStatus):
            response = error.response
            if response.status_code in (429, 418):
                try:
                    delay = _retry_delay(response)
                except ValueError:
                    delay = 180 if response.status_code == 418 else 60
                return AccountReadUnavailable("rate_limit", retry_after_seconds=delay)
            if response.status_code in (401, 403):
                return AccountReadUnavailable("authentication")
        return InvalidHandshake("private stream redirects are disabled")


def _decode(frame: str | bytes) -> dict:
    if type(frame) not in (str, bytes) or len(frame) > 262144:
        raise ValueError("invalid private stream frame size")
    if type(frame) is str and len(frame.encode("utf-8")) > 262144:
        raise ValueError("invalid private stream byte size")
    result = json.loads(frame)
    if type(result) is not dict:
        raise ValueError("invalid private stream JSON shape")
    return result


class BinanceUserStream:
    def __init__(
        self,
        rest: ReadOnlyClient,
        clock: ClockPort,
        account_ref: str,
        credentials: HmacCredentials | None,
        *,
        connect: Callable = _UserConnect,
    ):
        if credentials is not None and not isinstance(credentials, HmacCredentials):
            raise ValueError("unsupported subscription credentials")
        self.rest, self.clock, self.account_ref = rest, clock, live_account_ref(account_ref)
        self._credentials, self._connect = credentials, connect
        self._running, self._auth_rejected = False, False
        self._blocked_until = 0.0
        self._logger = logging.Logger("binance_private_stream_transport")
        # The transport may log complete JSON frames. Runtime exposes sanitized
        # typed failures instead; raw SDK logging is disabled for this socket.
        self._logger.disabled = True

    def _signal(self, kind: str, *, identity: str | None = None) -> AccountSignal:
        return AccountSignal(
            event_id=identity or "user-reconcile:" + str(uuid4()),
            account_ref=self.account_ref,
            kind=kind,
            received_at=self.clock.utcnow(),
        )

    async def updates(self) -> AsyncIterator[AccountSignal]:
        if self._credentials is None:
            raise AccountReadUnavailable("credentials")
        if self._auth_rejected:
            raise AccountReadUnavailable("authentication")
        remaining = self._blocked_until - self.clock.monotonic()
        if remaining > 0:
            raise AccountReadUnavailable("rate_limit", retry_after_seconds=ceil(remaining))
        if self._running:
            raise RuntimeError("private stream already has a consumer")
        self._running = True
        try:
            async with self._connect(
                USER_STREAM_URL,
                proxy=None,
                compression=None,
                max_size=262144,
                max_queue=32,
                open_timeout=10,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                logger=self._logger,
            ) as socket:
                with _port_errors():
                    time = await self.rest.request("GET", "/api/v3/time")
                milliseconds = time["serverTime"]
                if type(milliseconds) is not int or not 0 <= milliseconds <= 2**63 - 1:
                    raise ValueError("invalid subscription server clock")
                params = {
                    "apiKey": self._credentials.api_key.get_secret_value(),
                    "timestamp": milliseconds,
                    "recvWindow": 5000,
                }
                payload = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
                params["signature"] = hmac_signature(payload, self._credentials.secret_key)
                packet = {
                    "id": _REQUEST_ID,
                    "method": "userDataStream.subscribe.signature",
                    "params": params,
                }
                async with asyncio.timeout(10):
                    await socket.send(json.dumps(packet))
                    ack = _decode(await socket.recv())
                status = ack.get("status")
                if status in (429, 418):
                    retry_at = ack.get("error", {}).get("retryAfter")
                    delay = 180 if status == 418 else 60
                    if type(retry_at) is int and retry_at > milliseconds:
                        candidate = ceil((retry_at - milliseconds) / 1000)
                        if candidate <= 999999999:
                            delay = candidate
                    self._blocked_until = self.clock.monotonic() + delay
                    raise AccountReadUnavailable("rate_limit", retry_after_seconds=delay)
                if status in (401, 403) or ack.get("error", {}).get("code") in (
                    -2014,
                    -2015,
                    -1022,
                ):
                    self._auth_rejected = True
                    raise AccountReadUnavailable("authentication")
                if ack.get("id") != _REQUEST_ID or type(status) is not int or status != 200:
                    raise ValueError("subscription acknowledgment mismatch")
                subscription = ack["result"]["subscriptionId"]
                if type(subscription) is not int or not 0 <= subscription <= 65535:
                    raise ValueError("invalid subscription identity")
                expiry = self.clock.monotonic() + 85800
                yield self._signal("reconcile_required")
                while True:
                    remaining = expiry - self.clock.monotonic()
                    if remaining <= 0:
                        raise AccountReadUnavailable("transport")
                    async with asyncio.timeout(remaining):
                        raw = _decode(await socket.recv())
                    if (
                        type(raw.get("subscriptionId")) is not int
                        or raw["subscriptionId"] != subscription
                    ):
                        raise ValueError("private event subscription mismatch")
                    event = raw["event"]
                    if type(event) is not dict or type(event.get("e")) is not str:
                        raise ValueError("invalid private event")
                    name = event["e"]
                    if name == "executionReport":
                        if event.get("s") != "BTCUSDT":
                            continue
                        kind = "execution_changed"
                    elif name in ("outboundAccountPosition", "balanceUpdate", "externalLockUpdate"):
                        kind = "balance_changed"
                    elif name == "eventStreamTerminated":
                        raise AccountReadUnavailable("transport")
                    else:
                        continue
                    digest = hashlib.sha256(
                        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    yield self._signal(kind, identity="binance-user:" + digest)
        except AccountReadUnavailable as error:
            if error.reason == "rate_limit":
                self._blocked_until = max(
                    self._blocked_until, self.clock.monotonic() + error.retry_after_seconds
                )
            elif error.reason == "authentication":
                self._auth_rejected = True
            raise
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            UnicodeError,
            RecursionError,
            OverflowError,
        ):
            raise AccountReadUnavailable("invalid_data") from None
        except (WebSocketException, OSError, TimeoutError):
            raise AccountReadUnavailable("transport") from None
        finally:
            self._running = False
