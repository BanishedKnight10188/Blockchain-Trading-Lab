"""The actual Agent adapter consumes standard function-call HTTP responses."""

import json
from importlib import import_module

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.routing import ModelPrice
from agent_platform.ports.model import ModelCallFailed
from tests.fixtures.event_agent_cases import request
from tests.fixtures.watch_cases import NOW


class Client:
    def __init__(self, values):
        self.values, self.payloads = list(values), []

    async def post(self, path, payload, deadline):
        assert path == "/api/v1/chat/completions"
        self.payloads.append(payload)
        return self.values.pop(0)


def provider(message, finish="tool_calls", **changes):
    return {
        "id": "generation-1",
        "model": "test/model",
        "provider": "test-provider",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": "0"},
        "choices": [{"finish_reason": finish, "message": {"role": "assistant", **message}}],
        **changes,
    }


def model(client):
    cls = import_module("agent_platform.adapters.openrouter.agent_chat").OpenRouterAgentModel
    price = ModelPrice(
        version="test-price",
        input_usd_per_million="0.000001",
        output_usd_per_million="0.000001",
        verified_at=NOW,
    )
    return cls(
        client, FakeClock(NOW), model_id="test/model", providers=("test-provider",), price=price
    )


@pytest.mark.asyncio
async def test_tool_call_protocol():
    client = Client(
        [
            provider(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "get_market_snapshot", "arguments": "{}"},
                        }
                    ],
                }
            ),
            provider({"content": json.dumps({"action": "WAIT", "reason": "No entry"})}, "stop"),
        ]
    )
    port = model(client)
    first = await port.turn(request())
    second = await port.turn(
        request(
            request_id="second",
            messages=[
                {"role": "user", "content": "Inspect"},
                {"role": "assistant", "tool_calls": first.tool_calls},
                {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
            ],
        )
    )
    assert client.payloads[1]["messages"][-1]["tool_call_id"] == "call-1"
    assert second.usage.request_id == "second" and second.final.action == "WAIT"
    assert client.payloads[1]["tools"] and not client.payloads[1]["provider"]["allow_fallbacks"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        provider({"content": "{}"}, "stop", model="another/model"),
        provider(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {"name": "get_market_snapshot", "arguments": "[]"},
                    }
                ],
            }
        ),
        provider(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {"name": "unknown", "arguments": "{}"},
                    }
                ],
            }
        ),
    ],
)
async def test_wrong_model_or_malformed_protocol_rejected(value):
    with pytest.raises(ModelCallFailed):
        await model(Client([value])).turn(request())
