"""Bounded inference; one proven pre-send reconnect, never replay a submitted POST."""

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from decimal import Decimal

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from agent_platform.domain.common import utc_datetime
from agent_platform.domain.model_diagnostics import ModelTransportEvidence
from agent_platform.ports.clock import ClockPort

_PATHS = frozenset({"/api/v1/chat/completions", "/api/alpha/decisions"})
_ACTIVE_KEY: ContextVar[str | None] = ContextVar("openrouter_log_key", default=None)


class OpenRouterError(RuntimeError):
    def __init__(self, reason, *, transport_evidence=None):
        self.transport_evidence = transport_evidence
        super().__init__(reason)


class _TransportResult(dict):
    def __init__(self, value, evidence):
        super().__init__(value)
        self.transport_evidence = evidence


class _Trace:
    """Per-call trace. Never retain headers, request bodies or exception text."""

    def __init__(self):
        self.started = asyncio.get_running_loop().time()
        self.attempts = 0
        self.request_started = False
        self.phase = "unknown"
        self.hop = None
        self.observed_connection = False
        self.pending = {}
        self.durations = dict.fromkeys(
            ("connect", "proxy_connect", "tls", "send", "response_wait", "body"), 0.0
        )

    def begin_attempt(self):
        self.attempts += 1
        self.hop = None
        self.phase = "unknown"
        self.observed_connection = False

    async def __call__(self, name, info):
        base, _, event = name.rpartition(".")
        phase = None
        if base == "connection.connect_tcp":
            phase = "connect"
            self.observed_connection |= event == "started"
        elif base in ("connection.start_tls", "proxy.start_tls"):
            phase = "tls"
            self.observed_connection |= event == "started"
        elif base.startswith(("http11.", "http2.")):
            if event == "started" and base.endswith("send_request_headers"):
                method = getattr(info.get("request"), "method", None)
                self.hop = "proxy" if method == b"CONNECT" else "inference"
                # Missing method evidence is conservatively treated as a sent POST.
                if self.hop == "inference":
                    self.request_started = True
            if self.hop == "proxy":
                phase = "proxy_connect"
            elif self.hop == "inference":
                if base.endswith(("send_request_headers", "send_request_body")):
                    phase = "send"
                elif base.endswith("receive_response_headers"):
                    phase = "response_wait"
                elif base.endswith("receive_response_body"):
                    phase = "body"
        now = asyncio.get_running_loop().time()
        if phase is not None:
            if event == "started":
                self.phase = phase
                self.pending[base] = (phase, now)
            elif event in ("complete", "failed"):
                pending = self.pending.pop(base, None)
                if pending is not None:
                    self.durations[pending[0]] += now - pending[1]

    def can_reconnect(self):
        return (
            self.attempts == 1
            and not self.request_started
            and self.observed_connection
            and self.phase in ("connect", "tls")
        )

    def evidence(self, error_type=None):
        now = asyncio.get_running_loop().time()
        durations = self.durations.copy()
        for phase, began in self.pending.values():
            durations[phase] += now - began
        # Only exception class names from a fixed vocabulary can reach persistence.
        safe_error = (
            error_type
            if error_type
            in {
                "ConnectError",
                "ConnectTimeout",
                "ReadError",
                "ReadTimeout",
                "WriteError",
                "WriteTimeout",
                "PoolTimeout",
                "ProxyError",
                "RemoteProtocolError",
                "LocalProtocolError",
                "DeadlineTimeout",
            }
            else "HTTPError"
            if error_type
            else None
        )
        return ModelTransportEvidence(
            attempts=self.attempts,
            pre_request_retries=self.attempts - 1,
            request_started=self.request_started,
            **{key + "_ms": round(value * 1000) for key, value in durations.items()},
            total_ms=round((now - self.started) * 1000),
            failed_phase=self.phase if error_type else None,
            error_type=safe_error,
        )


class OpenRouterCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def bounded_key(cls, value):
        if re.fullmatch(r"[A-Za-z0-9_-]{8,256}", value.get_secret_value()) is None:
            raise ValueError("invalid OpenRouter credential")
        return value


class _CredentialFilter(logging.Filter):
    def filter(self, record):
        key = _ACTIVE_KEY.get()
        if key:
            record.msg = record.getMessage().replace(key, "[REDACTED]")
            record.args = ()
        return True


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON")


def _contains_credential(value, key):
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is str:
            # Also inspect escaped strings inside JSON content before later parsing.
            while True:
                if key in item:
                    return True
                decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), item)
                if decoded == item:
                    break
                item = decoded
    return False


class OpenRouterClient:
    def __init__(
        self, clock: ClockPort, *, credentials=None, enabled=False, client=None, proxy_url=None
    ):
        if proxy_url is not None:
            from agent_platform.config import RuntimeConfig

            RuntimeConfig(openrouter_proxy=proxy_url)
            if client is not None:
                raise ValueError("configure proxy on the borrowed HTTP client explicitly")
        if type(enabled) is not bool or (
            enabled and not isinstance(credentials, OpenRouterCredentials)
        ):
            raise ValueError("explicit inference switch and credentials are required")
        self.clock, self.credentials, self.enabled = clock, credentials, enabled
        self._owned = enabled and client is None
        self._client = client
        if self._owned:
            self._client = httpx.AsyncClient(
                proxy=proxy_url,
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(keepalive_expiry=60),
            )
        for name in (
            "httpx",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
        ):
            logger = logging.getLogger(name)
            if not any(isinstance(item, _CredentialFilter) for item in logger.filters):
                logger.addFilter(_CredentialFilter())

    async def post(self, path: str, payload: dict, deadline, *, max_wait_seconds=15) -> dict:
        if not self.enabled:
            raise OpenRouterError("model_calls_disabled")
        if type(path) is not str or path not in _PATHS or type(payload) is not dict:
            raise OpenRouterError("unsupported_model_endpoint")
        if (
            type(max_wait_seconds) is not int
            or not 1 <= max_wait_seconds <= 60
            or (max_wait_seconds != 15 and path != "/api/v1/chat/completions")
        ):
            raise OpenRouterError("invalid_model_request")
        try:
            remaining = min(
                max_wait_seconds, (utc_datetime(deadline) - self.clock.utcnow()).total_seconds()
            )
            raw = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode()
            if remaining <= 0 or not 1 <= len(raw) <= 131072:
                raise ValueError("request outside bounds")
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise OpenRouterError("invalid_model_request") from None
        key = self.credentials.api_key.get_secret_value()
        if key.encode() in raw or _contains_credential(payload, key):
            raise OpenRouterError("invalid_model_request")
        token = _ACTIVE_KEY.set(key)
        trace = _Trace()
        try:
            async with asyncio.timeout(remaining):
                for attempt in range(2):
                    trace.begin_attempt()
                    # Failed events normally finish the pending trace duration.
                    # Also settle borrowed transports which raise before .failed.
                    try:
                        return await self._post_once(path, raw, key, remaining, trace)
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if attempt == 0 and trace.can_reconnect():
                            now = asyncio.get_running_loop().time()
                            for phase, began in trace.pending.values():
                                trace.durations[phase] += now - began
                            trace.pending.clear()
                            continue
                        raise
        except (TimeoutError, httpx.TimeoutException) as error:
            raise OpenRouterError(
                "provider_timeout",
                transport_evidence=trace.evidence(
                    "DeadlineTimeout" if isinstance(error, TimeoutError) else type(error).__name__
                ),
            ) from None
        except httpx.HTTPError as error:
            raise OpenRouterError(
                "provider_transport_error", transport_evidence=trace.evidence(type(error).__name__)
            ) from None
        except OpenRouterError as error:
            if error.transport_evidence is None:
                error.transport_evidence = trace.evidence()
            raise
        except (
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
            OverflowError,
        ):
            raise OpenRouterError("invalid_model_response") from None
        finally:
            _ACTIVE_KEY.reset(token)

    async def _post_once(self, path, raw, key, remaining, trace):
        async with self._client.stream(
            "POST",
            "https://openrouter.ai" + path,
            content=raw,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
            timeout=remaining,
            follow_redirects=False,
            extensions={"trace": trace},
        ) as response:
            if response.status_code == 403:
                # Keep the refusal distinct without exposing arbitrary
                # provider text or treating it as evidence of zero cost.
                raise OpenRouterError("provider_access_denied")
            if response.status_code != 200:
                # Only the numeric status is retained. Arbitrary error
                # bodies and headers are never included in a failure.
                raise OpenRouterError(f"provider_http_{response.status_code}")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise OpenRouterError("invalid_model_response")
            if response.headers.get("content-type", "").split(";")[0].lower() != "application/json":
                raise OpenRouterError("invalid_model_response")
            content = bytearray()
            if response.is_stream_consumed:
                if len(response.content) > 262144:
                    raise OpenRouterError("model_response_too_large")
                content.extend(response.content)
            else:
                async for chunk in response.aiter_raw():
                    if len(content) + len(chunk) > 262144:
                        raise OpenRouterError("model_response_too_large")
                    content.extend(chunk)
            if key.encode() in content:
                raise OpenRouterError("invalid_model_response")
            value = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=Decimal,
            )
            if type(value) is not dict or _contains_credential(value, key):
                raise ValueError("invalid JSON shape")
            return _TransportResult(value, trace.evidence())

    async def aclose(self):
        if self._owned and self._client is not None:
            await self._client.aclose()
