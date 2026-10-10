import asyncio
import importlib
import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from agent_platform.adapters.fake.clock import FakeClock
from tests.domain.test_decisions import NOW

KEY = "sk-or-v1-offline-never-export"


def module():
    return importlib.import_module("agent_platform.adapters.openrouter.transport")


def client(http, *, enabled=True):
    return module().OpenRouterClient(
        FakeClock(NOW),
        credentials=module().OpenRouterCredentials(api_key=KEY),
        enabled=enabled,
        client=http,
    )


def test_explicit_proxy_cannot_be_silently_ignored_by_a_borrowed_client():
    with pytest.raises(ValueError, match="borrowed"):
        module().OpenRouterClient(
            FakeClock(NOW),
            credentials=module().OpenRouterCredentials(api_key=KEY),
            enabled=True,
            client=object(),
            proxy_url="http://127.0.0.1:7897",
        )


def test_openrouter_proxy_is_independent_and_rejects_credential_bearing_urls():
    from agent_platform.config import RuntimeConfig

    selected = RuntimeConfig(openrouter_proxy="http://127.0.0.1:7897")
    assert selected.futures_proxy is None
    for address in (
        "http://user:secret@127.0.0.1:7897",
        "http://outside.example:7897",
        "http://127.0.0.1:0",
        "http://127.0.0.1:7897/path",
    ):
        with pytest.raises(ValueError):
            RuntimeConfig(openrouter_proxy=address)


@pytest.mark.asyncio
async def test_disabled_transport_never_sends_even_with_credentials():
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as http:
        transport = client(http, enabled=False)
        with pytest.raises(module().OpenRouterError, match="disabled"):
            await transport.post("/api/v1/chat/completions", {}, NOW + timedelta(seconds=15))
        assert calls == []


@pytest.mark.asyncio
async def test_long_background_wait_is_explicit_and_cannot_extend_decisions():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = client(http)
        deadline = NOW + timedelta(seconds=60)
        await transport.post("/api/v1/chat/completions", {}, deadline, max_wait_seconds=60)
        assert seen[-1]["read"] == 60
        await transport.post("/api/alpha/decisions", {}, NOW + timedelta(seconds=3))
        assert seen[-1]["read"] == 3
        with pytest.raises(module().OpenRouterError, match="invalid_model_request"):
            await transport.post("/api/alpha/decisions", {}, deadline, max_wait_seconds=60)
        assert len(seen) == 2


@pytest.mark.asyncio
async def test_fixed_host_explicit_auth_no_redirect_and_borrowed_client_ownership():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        transport = client(http)
        assert await transport.post(
            "/api/alpha/decisions", {"state": "test"}, NOW + timedelta(seconds=15)
        ) == {"ok": True}
        assert str(seen[0].url) == "https://openrouter.ai/api/alpha/decisions"
        assert seen[0].headers["authorization"] == "Bearer " + KEY
        await transport.aclose()
        assert not http.is_closed
        assert KEY not in repr(transport) and KEY not in repr(transport.credentials)


@pytest.mark.parametrize(
    "path",
    ["https://evil/api/v1/chat/completions", "/api/v1/models", "/api/v1/chat/completions?key=x"],
)
@pytest.mark.asyncio
async def test_unapproved_paths_do_not_send(path):
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r))) as http:
        with pytest.raises(module().OpenRouterError):
            await client(http).post(path, {}, NOW + timedelta(seconds=15))
        assert not seen


@pytest.mark.parametrize("status", [302, 401, 429, 500])
@pytest.mark.asyncio
async def test_error_is_sanitized_and_never_retried_or_redirected(status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status, headers={"location": "https://evil/", "retry-after": "99999"}, text=KEY
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        with pytest.raises(module().OpenRouterError) as error:
            await client(http).post("/api/v1/chat/completions", {}, NOW + timedelta(seconds=15))
        assert len(seen) == 1 and KEY not in str(error.value)


@pytest.mark.asyncio
async def test_forbidden_is_distinct_and_does_not_echo_provider_body_or_retry():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(403, json={"error": {"code": 403, "message": KEY}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(module().OpenRouterError) as error:
            await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=15))
        assert str(error.value) == "provider_access_denied"
        assert len(seen) == 1 and KEY not in str(error.value)


@pytest.mark.asyncio
async def test_transport_failure_is_distinct_from_invalid_json_without_retry():
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ConnectError(KEY, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(module().OpenRouterError) as error:
            await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=15))
        assert str(error.value) == "provider_transport_error"
        assert len(seen) == 1 and KEY not in str(error.value)


async def trace_event(request, name, method=None):
    info = {"request": SimpleNamespace(method=method)} if method else {}
    await request.extensions["trace"](name, info)


@pytest.mark.asyncio
async def test_tls_failure_before_post_reconnects_once_and_retains_phase_evidence():
    seen = []

    async def handler(request):
        seen.append(request)
        await trace_event(request, "proxy.start_tls.started")
        if len(seen) == 1:
            await trace_event(request, "proxy.start_tls.failed")
            raise httpx.ConnectError(KEY, request=request)
        await trace_event(request, "proxy.start_tls.complete")
        await trace_event(request, "http11.send_request_headers.started", b"POST")
        await trace_event(request, "http11.send_request_headers.complete")
        await trace_event(request, "http11.receive_response_headers.started", b"POST")
        await trace_event(request, "http11.receive_response_headers.complete")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        value = await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=3))
    assert value == {"ok": True}
    evidence = value.transport_evidence
    assert evidence.attempts == 2 and evidence.pre_request_retries == 1
    assert evidence.failed_phase is None and evidence.error_type is None
    assert evidence.tls_ms >= 0 and evidence.response_wait_ms >= 0
    assert KEY not in evidence.model_dump_json()


@pytest.mark.parametrize("kind", [httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout])
@pytest.mark.asyncio
async def test_no_retry_after_post_headers_started_even_if_exception_says_connect(kind):
    seen = []

    async def handler(request):
        seen.append(request)
        await trace_event(request, "http11.send_request_headers.started", b"POST")
        await trace_event(request, "http11.receive_response_headers.started", b"POST")
        raise kind(KEY, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(module().OpenRouterError) as raised:
            await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=3))
    assert len(seen) == 1
    evidence = raised.value.transport_evidence
    assert evidence.request_started and evidence.failed_phase == "response_wait"
    assert evidence.error_type == kind.__name__
    assert KEY not in repr(raised.value) and KEY not in evidence.model_dump_json()


@pytest.mark.asyncio
async def test_two_tls_failures_pause_with_bounded_retry_and_safe_diagnostic():
    seen = []

    async def handler(request):
        seen.append(request)
        # CONNECT is the proxy tunnel, not a submitted inference request.
        await trace_event(request, "http11.send_request_headers.started", b"CONNECT")
        await trace_event(request, "http11.send_request_headers.complete")
        await trace_event(request, "proxy.start_tls.started")
        await trace_event(request, "proxy.start_tls.failed")
        raise httpx.ConnectError(KEY, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(module().OpenRouterError) as raised:
            await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=3))
    assert len(seen) == 2 and str(raised.value) == "provider_transport_error"
    evidence = raised.value.transport_evidence
    assert evidence.attempts == 2 and evidence.pre_request_retries == 1
    assert not evidence.request_started and evidence.failed_phase == "tls"
    assert evidence.error_type == "ConnectError"
    assert KEY not in evidence.model_dump_json()


@pytest.mark.asyncio
async def test_reconnect_shares_original_deadline_instead_of_extending_it():
    seen = []

    async def handler(request):
        seen.append(request)
        await trace_event(request, "proxy.start_tls.started")
        if len(seen) == 1:
            raise httpx.ConnectError(KEY, request=request)
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(module().OpenRouterError, match="provider_timeout") as raised:
            await asyncio.wait_for(
                client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=0.03)), 1
            )
    assert len(seen) == 2 and raised.value.transport_evidence.attempts == 2


@pytest.mark.asyncio
async def test_concurrent_requests_keep_separate_retry_and_phase_evidence():
    counts = {"cold": 0, "warm": 0}

    async def handler(request):
        label = json.loads(request.content)["label"]
        counts[label] += 1
        if label == "cold":
            await trace_event(request, "proxy.start_tls.started")
            await asyncio.sleep(0)
            await trace_event(
                request,
                "proxy.start_tls.failed" if counts[label] == 1 else "proxy.start_tls.complete",
            )
            if counts[label] == 1:
                raise httpx.ConnectError(KEY, request=request)
        await trace_event(request, "http11.send_request_headers.started", b"POST")
        await asyncio.sleep(0)
        await trace_event(request, "http11.send_request_headers.complete")
        return httpx.Response(200, json={"label": label})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = client(http)
        cold, warm = await asyncio.gather(
            *(
                transport.post("/api/alpha/decisions", {"label": label}, NOW + timedelta(seconds=3))
                for label in counts
            )
        )
    assert cold.transport_evidence.attempts == 2
    assert warm.transport_evidence.attempts == 1
    assert cold["label"] == "cold" and warm["label"] == "warm"


@pytest.mark.parametrize(
    "body",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b"[]",
        b"not-json",
        b"{}" + b" " * 262144,
        ('{"key":"' + KEY + '"}').encode(),
    ],
    ids=["duplicate", "nan", "array", "malformed", "oversized", "key-echo"],
)
@pytest.mark.asyncio
async def test_invalid_or_oversized_response_is_rejected(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        )
    ) as http:
        with pytest.raises(module().OpenRouterError) as error:
            await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=15))
        assert KEY not in str(error.value)


@pytest.mark.asyncio
async def test_expired_or_oversized_input_does_not_send():
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r))) as http:
        transport = client(http)
        for payload, deadline in (
            ({}, NOW),
            ({"state": "x" * 131072}, NOW + timedelta(seconds=15)),
        ):
            with pytest.raises(module().OpenRouterError):
                await transport.post("/api/alpha/decisions", payload, deadline)
        assert not seen


@pytest.mark.asyncio
async def test_credential_in_payload_is_rejected_before_send():
    seen = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (seen.append(r), httpx.Response(200, json={"ok": True}))[1]
        )
    ) as http:
        with pytest.raises(module().OpenRouterError):
            await client(http).post(
                "/api/alpha/decisions", {"state": KEY}, NOW + timedelta(seconds=15)
            )
        assert not seen


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.asyncio
async def test_unicode_escaped_credential_echo_is_not_returned(nested):
    escaped = "".join(f"\\u{ord(c):04x}" for c in KEY)
    body = '{"echo":"' + escaped + '"}'
    if nested:
        body = json.dumps({"choices": [{"message": {"content": body}}]})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=body.encode(), headers={"content-type": "application/json"}
            )
        )
    ) as http:
        with pytest.raises(module().OpenRouterError) as error:
            await client(http).post("/api/v1/chat/completions", {}, NOW + timedelta(seconds=15))
        assert KEY not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_is_one_attempt_and_key_logs_are_redacted(caplog):
    seen = []

    def handler(request):
        seen.append(request)
        logging.getLogger("httpcore.http11").debug(
            "request headers %r", {"Authorization": "Bearer " + KEY}
        )
        raise httpx.ReadTimeout(KEY, request=request)

    caplog.set_level(logging.DEBUG, logger="httpcore.http11")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(module().OpenRouterError, match="timeout") as error:
            await client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=15))
        assert len(seen) == 1 and KEY not in str(error.value) and KEY not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_closes_owned_response_stream():
    entered = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            self.closed = True

    stream = Stream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, stream=stream, headers={"content-type": "application/json"}
            )
        )
    ) as http:
        task = asyncio.create_task(
            client(http).post("/api/alpha/decisions", {}, NOW + timedelta(seconds=15))
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
