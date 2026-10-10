"""Model replies are finite owned data and must identify the matching request."""

import importlib
from datetime import timedelta

import pytest

from tests.domain.test_decisions import NOW, snapshot_data


def request_data():
    return {
        "request_id": "request-1",
        "snapshot": snapshot_data(),
        "purpose": "advisory",
        "route": {
            "route_id": "fake-route",
            "kind": "economy",
            "purpose": "advisory",
            "reason": "离线验证",
            "model_version": "fake-v1",
            "paid": False,
        },
        "deadline": NOW + timedelta(seconds=15),
        "max_output_tokens": 500,
        "prompt_version": "test-prompt-v1",
    }


def test_model_request_has_no_provider_secret_or_arbitrary_prompt_dictionary():
    models = importlib.import_module("agent_platform.domain.model_calls")
    request = models.ModelRequest(**request_data())
    assert models.ModelRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValueError):
        models.ModelRequest(**request_data(), api_secret="never-prompt")
    with pytest.raises(ValueError):
        models.ModelRequest(**{**request_data(), "deadline": NOW})


def test_model_response_rejects_mismatched_usage_identity_and_human_authorship():
    models = importlib.import_module("agent_platform.domain.model_calls")
    base = {
        "request_id": "request-1",
        "assessment": {"action": "hold", "explanation": "离线验证", "source": "fake"},
        "usage": {
            "request_id": "request-1",
            "route_id": "fake-route",
            "model_version": "fake-v1",
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": "0",
            "actual_cost_usd": "0",
            "billing_status": "confirmed",
            "recorded_at": NOW,
        },
    }
    response = models.ModelResponse(**base)
    assert response.usage.actual_cost_usd == 0
    with pytest.raises(ValueError):
        models.ModelResponse(**{**base, "request_id": "request-2"})
    with pytest.raises(ValueError):
        models.ModelResponse(**{**base, "assessment": {**base["assessment"], "source": "human"}})
