import importlib
import json
from decimal import Decimal

import httpx
import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.openrouter.transport import OpenRouterClient, OpenRouterCredentials
from agent_platform.application.prompting import build_prompt
from agent_platform.application.routing import ModelRouter
from agent_platform.domain.model_calls import ModelRequest
from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.domain.routing import RoutingPolicy
from tests.application.test_prompting import output, request
from tests.application.test_routing import tier
from tests.domain.test_decisions import NOW

MODEL = "anthropic/claude-haiku-5.5"


def model_module():
    return importlib.import_module("agent_platform.adapters.openrouter.chat")


def setup_request(model_id=MODEL, *, overhead=4096):
    clock = FakeClock(NOW)
    router = ModelRouter(
        RoutingPolicy(
            routes=(tier(model_version=model_id, prompt_overhead_tokens=overhead),),
            daily_limit_usd="1",
        ),
        clock,
    )
    data = request().model_dump()
    data["route"] = router.select(request().snapshot, "advisory")
    data["max_output_tokens"] = 100
    return clock, router, ModelRequest(**data)


def response(**changes):
    result = {
        "id": "gen-fixture-1",
        "model": MODEL,
        "provider": "Fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"content": output().decode(), "role": "assistant"},
            }
        ],
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 10},
            "cost": 0.0004,
        },
    }
    result.update(changes)
    return result


async def generate(payload, *, model_id=MODEL, modules=None):
    clock, router, req = setup_request(model_id)
    seen = []

    def handler(r):
        seen.append(json.loads(r.content))
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(
            clock,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-chat-key"),
            enabled=True,
            client=http,
        )
        adapter = model_module().OpenRouterChatModel(
            client, router, clock, modules=modules or ModelModulesConfig(), providers=("fixture",)
        )
        return await adapter.generate(req), seen


@pytest.mark.asyncio
async def test_chat_preserves_total_billed_tokens_and_metadata_with_strict_request():
    reply, seen = await generate(response())
    assert reply.usage.actual_cost_usd == Decimal("0.0004")
    assert reply.usage.output_tokens == 20  # reasoning is already included; do not add twice.
    assert reply.provider_metadata.model_id == MODEL
    assert reply.provider_metadata.request_id == "gen-fixture-1"
    assert reply.assessment.action == "buy"
    body = seen[0]
    assert body["model"] == MODEL and body["stream"] is False
    assert body["provider"]["only"] == ["fixture"]
    assert body["provider"]["require_parameters"] is True
    assert body["provider"]["allow_fallbacks"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "private-account-never-export" not in json.dumps(body)


@pytest.mark.asyncio
async def test_quote_covers_complete_wire_schema_and_provider_price_is_numeric():
    reply, seen = await generate(response())
    _, router, req = setup_request()
    quote = router.quote(req.route, build_prompt(req))
    assert (
        quote.input_token_upper_bound
        >= len(json.dumps(seen[0], ensure_ascii=False, separators=(",", ":")).encode()) + 1024
    )
    assert type(seen[0]["provider"]["max_price"]["prompt"]) in (int, float)


@pytest.mark.asyncio
async def test_underquoted_wire_request_is_rejected_with_known_zero_fee_before_send():
    clock, router, req = setup_request(overhead=100)
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r))) as http:
        client = OpenRouterClient(
            clock,
            enabled=True,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-chat-key"),
            client=http,
        )
        adapter = model_module().OpenRouterChatModel(client, router, clock, providers=("fixture",))
        with pytest.raises(
            importlib.import_module("agent_platform.ports.model").ModelCallFailed
        ) as error:
            await adapter.generate(req)
        assert seen == [] and error.value.usage.actual_cost_usd == 0


@pytest.mark.asyncio
async def test_missing_actual_cost_is_unknown_never_zero():
    value = response()
    value["usage"].pop("cost")
    reply, _ = await generate(value)
    assert reply.usage.billing_status == "unknown"
    assert reply.usage.actual_cost_usd is None


@pytest.mark.parametrize("change", ["content", "truncated", "model", "refusal", "message_shape"])
@pytest.mark.asyncio
async def test_bad_assessment_or_metadata_carries_confirmed_fee(change):
    value = response()
    if change == "content":
        value["choices"][0]["message"]["content"] = "not-json"
    elif change == "truncated":
        value["choices"][0]["finish_reason"] = "length"
    elif change == "refusal":
        value["choices"][0]["message"]["refusal"] = "refused"
    elif change == "message_shape":
        value["choices"][0]["message"] = []
    else:
        value["model"] = "vendor/unrequested-model"
    with pytest.raises(Exception) as error:
        await generate(value)
    assert isinstance(
        error.value, importlib.import_module("agent_platform.ports.model").ModelCallFailed
    )
    assert error.value.usage.actual_cost_usd == Decimal("0.0004")


@pytest.mark.asyncio
async def test_selected_strong_route_is_rejected_before_any_send():
    clock, router, req = setup_request("vendor/reviewer-v1")
    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r))) as http:
        client = OpenRouterClient(
            clock,
            enabled=True,
            credentials=OpenRouterCredentials(api_key="sk-or-v1-offline-chat-key"),
            client=http,
        )
        adapter = model_module().OpenRouterChatModel(
            client,
            router,
            clock,
            modules=ModelModulesConfig(strong_model={"model_id": "vendor/reviewer-v1"}),
            providers=("fixture",),
        )
        with pytest.raises(importlib.import_module("agent_platform.ports.model").ModelCallFailed):
            await adapter.generate(req)
        assert seen == []


@pytest.mark.parametrize(
    "change",
    [{"prompt_tokens": True}, {"completion_tokens": 101}, {"cost": -1}, {"cost": "1e999999"}],
)
@pytest.mark.asyncio
async def test_invalid_usage_is_rejected_without_inventing_a_bill(change):
    value = response()
    value["usage"].update(change)
    with pytest.raises(Exception) as error:
        await generate(value)
    assert isinstance(
        error.value, importlib.import_module("agent_platform.ports.model").ModelCallFailed
    )
    assert error.value.usage is None
