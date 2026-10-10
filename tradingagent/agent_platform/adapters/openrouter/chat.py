"""One explicitly selected analysis model; strong selection never grants access."""

import json
import re
from decimal import Decimal
from math import inf, nextafter

from agent_platform.application.prompting import build_prompt, parse_assessment
from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.model_calls import ModelRequest, ModelResponse
from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.ports.model import ModelCallFailed

from .parsing import read_metadata, read_usage
from .transport import OpenRouterError


def _provider_price(value: Decimal) -> float:
    # JSON numbers are required here; never round a provider ceiling upward.
    number = float(value)
    if Decimal(str(number)) > value:
        number = nextafter(number, -inf)
    return number


def _schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "action",
            "explanation",
            "source",
            "evidence_ids",
            "quantity",
            "unavailable_reasons",
            "valid_for_seconds",
        ],
        "properties": {
            "action": {"type": "string", "enum": ["buy", "sell", "hold", "unavailable"]},
            "explanation": {"type": "string", "minLength": 1, "maxLength": 2048},
            "source": {"type": "string", "enum": ["model"]},
            "evidence_ids": {
                "type": "array",
                "maxItems": 64,
                "items": {"type": "string", "maxLength": 128},
            },
            "quantity": {
                "anyOf": [
                    {"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]+)?$", "maxLength": 128},
                    {"type": "null"},
                ]
            },
            "unavailable_reasons": {
                "type": "array",
                "maxItems": 16,
                "items": {"type": "string", "maxLength": 128},
            },
            "valid_for_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        },
    }


class OpenRouterChatModel:
    def __init__(self, client, router, clock, *, modules=None, providers):
        if (
            type(providers) is not tuple
            or not 1 <= len(providers) <= 8
            or len(set(providers)) != len(providers)
            or any(
                type(p) is not str or re.fullmatch(r"[a-z0-9._-]{1,64}", p) is None
                for p in providers
            )
        ):
            raise ValueError("explicit bounded provider allowlist is required")
        self.client, self.router, self.clock = client, router, clock
        self.modules = ModelModulesConfig.model_validate_json(
            (modules or ModelModulesConfig()).model_dump_json()
        )
        self.providers = providers

    def check_request(self, request: ModelRequest) -> None:
        request = ModelRequest.model_validate_json(request.model_dump_json())
        if request.route.model_version != self.modules.analysis_model:
            raise ModelCallFailed("model_module_disabled")

    async def generate(self, request: ModelRequest) -> ModelResponse:
        request = ModelRequest.model_validate_json(request.model_dump_json())
        self.check_request(request)
        try:
            prompt = build_prompt(request)
            quote = self.router.quote(request.route, prompt)
            tier = next(t for t in self.router.policy.routes if t.kind == request.route.kind)
        except (ValueError, StopIteration):
            raise ModelCallFailed("quote_unavailable") from None
        data = json.loads(prompt)
        payload = {
            "model": self.modules.analysis_model,
            "stream": False,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": data["instruction"]},
                {
                    "role": "user",
                    "content": json.dumps(data["data"], ensure_ascii=False, separators=(",", ":")),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "btc_advisory", "strict": True, "schema": _schema()},
            },
            "provider": {
                "only": list(self.providers),
                "require_parameters": True,
                "allow_fallbacks": False,
                "max_price": {
                    "prompt": _provider_price(tier.price.input_usd_per_million),
                    "completion": _provider_price(tier.price.output_usd_per_million),
                },
            },
        }
        wire_size = len(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        )
        if (
            quote.input_token_upper_bound < wire_size + 1024
            or quote.max_output_tokens != request.max_output_tokens
        ):
            raise ModelCallFailed(
                "quote_unavailable",
                ModelUsage(
                    request_id=request.request_id,
                    route_id=request.route.route_id,
                    model_version=request.route.model_version,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost_usd=quote.estimated_cost_usd,
                    actual_cost_usd="0",
                    billing_status="confirmed",
                    recorded_at=self.clock.utcnow(),
                ),
            )
        try:
            value = await self.client.post("/api/v1/chat/completions", payload, request.deadline)
        except OpenRouterError:
            raise ModelCallFailed("provider_error") from None
        usage = read_usage(value, request, quote, self.clock.utcnow())
        metadata = read_metadata(value, self.modules.analysis_model, usage)
        try:
            choices = value["choices"]
            if (
                type(choices) is not list
                or len(choices) != 1
                or choices[0]["finish_reason"] != "stop"
            ):
                raise ValueError("incomplete answer")
            message = choices[0]["message"]
            if (
                type(message) is not dict
                or message.get("role") != "assistant"
                or message.get("refusal")
                or message.get("tool_calls")
                or type(message["content"]) is not str
            ):
                raise ValueError("invalid answer channel")
            assessment = parse_assessment(message["content"].encode(), request)
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ModelCallFailed("invalid_model_assessment", usage) from None
        return ModelResponse(
            request_id=request.request_id,
            assessment=assessment,
            usage=usage,
            provider_metadata=metadata,
        )
