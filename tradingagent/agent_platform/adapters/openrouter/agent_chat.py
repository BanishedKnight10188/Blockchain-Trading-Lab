"""Standard tool-call protocol; the legacy advisory adapter remains unchanged."""

import json
import re
from decimal import Context, Decimal, localcontext

from agent_platform.domain.agent_tools import ToolCall
from agent_platform.domain.event_agent import (
    AgentFinalDecision,
    AgentTurnRequest,
    AgentTurnResponse,
)
from agent_platform.domain.routing import ModelCostQuote, ModelPrice
from agent_platform.ports.model import ModelCallFailed

from .chat import _provider_price
from .parsing import read_metadata, read_usage
from .transport import OpenRouterError


def strict_object(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value

    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )
    if not isinstance(value, dict):
        raise ValueError("tool/final content must be an object")
    return value


def wire_messages(request):
    messages = []
    for item in request.messages:
        value = {"role": item.role, "content": item.content}
        if item.tool_call_id is not None:
            value["tool_call_id"] = item.tool_call_id
        if item.tool_calls:
            value["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, allow_nan=False, separators=(",", ":")
                        ),
                    },
                }
                for call in item.tool_calls
            ]
        if item.reasoning_details:
            value["reasoning_details"] = list(item.reasoning_details)
        messages.append(value)
    return messages


def agent_cost_quote(request, price, payload_bytes):
    with localcontext(Context(prec=80)):
        upper = payload_bytes + 1024
        estimate = (
            price.input_usd_per_million * upper
            + price.output_usd_per_million * request.max_output_tokens
        ) / Decimal(1000000)
    return ModelCostQuote(
        route_id=request.route.route_id,
        price_version=price.version,
        input_token_upper_bound=upper,
        max_output_tokens=request.max_output_tokens,
        estimated_cost_usd=estimate,
    )


class OpenRouterAgentModel:
    paid = True

    def __init__(self, client, clock, *, model_id, providers, price: ModelPrice):
        if (
            not providers
            or len(providers) > 8
            or len(set(providers)) != len(providers)
            or any(re.fullmatch(r"[a-z0-9._-]{1,64}", p) is None for p in providers)
        ):
            raise ValueError("explicit provider allowlist required")
        self.client, self.clock, self.model_id = client, clock, model_id
        self.providers, self.price = (
            providers,
            ModelPrice.model_validate_json(price.model_dump_json()),
        )

    def payload(self, request):
        request = AgentTurnRequest.model_validate_json(request.model_dump_json())
        if (
            request.route.model_version != self.model_id
            or request.route.price_version != self.price.version
            or not request.route.paid
            or not self.price.available_at(self.clock.utcnow())
        ):
            raise ModelCallFailed("agent_route_unavailable")
        return {
            "model": self.model_id,
            "stream": False,
            "max_tokens": request.max_output_tokens,
            "messages": wire_messages(request),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ],
            "provider": {
                "only": list(self.providers),
                "require_parameters": True,
                "allow_fallbacks": False,
                "max_price": {
                    "prompt": _provider_price(self.price.input_usd_per_million),
                    "completion": _provider_price(self.price.output_usd_per_million),
                },
            },
        }

    def quote(self, request):
        size = len(json.dumps(self.payload(request), ensure_ascii=False, allow_nan=False).encode())
        return agent_cost_quote(request, self.price, size)

    async def turn(self, request):
        request = AgentTurnRequest.model_validate_json(request.model_dump_json())
        payload, quote = self.payload(request), self.quote(request)
        try:
            value = await self.client.post("/api/v1/chat/completions", payload, request.deadline)
        except OpenRouterError:
            raise ModelCallFailed("provider_error") from None
        usage = read_usage(value, request, quote, self.clock.utcnow())
        metadata = read_metadata(value, self.model_id, usage)
        try:
            if metadata.provider.lower().replace(" ", "-") not in self.providers:
                raise ValueError("unexpected provider")
            choices = value["choices"]
            if type(choices) is not list or len(choices) != 1:
                raise ValueError("one choice required")
            choice = choices[0]
            message = choice["message"]
            if message.get("role") != "assistant" or message.get("refusal"):
                raise ValueError("wrong response channel")
            calls = message.get("tool_calls") or []
            final = None
            if calls:
                if (
                    choice["finish_reason"] != "tool_calls"
                    or type(calls) is not list
                    or len(calls) > 8
                ):
                    raise ValueError("invalid tool finish")
                parsed = []
                for call in calls:
                    if call["type"] != "function" or call["function"]["name"] not in {
                        t.name for t in request.tools
                    }:
                        raise ValueError("tool is not available")
                    parsed.append(
                        ToolCall(
                            tool_call_id=call["id"],
                            name=call["function"]["name"],
                            arguments=strict_object(call["function"]["arguments"]),
                        )
                    )
                calls = tuple(parsed)
            else:
                if choice["finish_reason"] != "stop" or type(message.get("content")) is not str:
                    raise ValueError("incomplete final decision")
                final = AgentFinalDecision.model_validate(strict_object(message["content"]))
            return AgentTurnResponse(
                request_id=request.request_id,
                tool_calls=calls,
                final=final,
                usage=usage,
                provider_metadata=metadata,
                reasoning_details=message.get("reasoning_details") or (),
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ModelCallFailed("invalid_agent_protocol", usage) from None
