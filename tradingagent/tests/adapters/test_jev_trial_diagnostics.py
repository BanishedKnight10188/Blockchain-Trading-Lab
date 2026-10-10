"""Diagnostic boundaries must distinguish pre-send, connection and response failures."""

import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.openrouter.transport import OpenRouterCredentials, OpenRouterError
from tests.domain.test_decisions import NOW

spec = importlib.util.spec_from_file_location(
    "jev_trial_tool", Path(__file__).resolve().parents[2] / "tools/run-jev-futures-trial.py"
)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)
KEY = "sk-or-v1-diagnostic-test-only"


@pytest.mark.parametrize("case", ["expired", "connect", "rejected", "malformed"])
@pytest.mark.asyncio
async def test_failure_boundaries_without_secret_or_retry(case):
    report, requests = {}, []

    async def handler(request):
        requests.append(request)
        if case == "connect":
            await request.extensions["trace"](
                "connection.start_tls.failed", {"exception": httpx.ConnectError(KEY)}
            )
            raise httpx.ConnectError(KEY, request=request)
        return httpx.Response(402 if case == "rejected" else 200, text=KEY)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={
            "request": [trial.provider_request_observer(report)],
            "response": [trial.provider_response_observer(report)],
        },
    ) as http:
        client = trial.DiagnosticOpenRouterClient(
            FakeClock(NOW),
            credentials=OpenRouterCredentials(api_key=KEY),
            enabled=True,
            client=http,
            report=report,
        )
        with pytest.raises(OpenRouterError):
            await client.post(
                "/api/alpha/decisions",
                {},
                NOW if case == "expired" else NOW + timedelta(seconds=15),
            )
    assert KEY not in json.dumps(report)
    assert len(requests) == (0 if case == "expired" else 1)
    if case == "expired":
        assert not report.get("http_request_prepared", False)
        assert report["provider_failure_code"] == "invalid_model_request"
    elif case == "connect":
        assert "provider_http_status" not in report
        assert report["http_trace"] == [
            {"event": "connection.start_tls.failed", "exception_type": "ConnectError"}
        ]
    else:
        assert report["provider_http_status"] == (402 if case == "rejected" else 200)
        assert report["provider_failure_code"] == (
            "provider_http_402" if case == "rejected" else "invalid_model_response"
        )
