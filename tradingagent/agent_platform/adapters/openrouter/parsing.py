"""Provider identity and cost parsing shared by the two distinct API surfaces."""

import re
from decimal import Decimal

from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.model_calls import ProviderMetadata
from agent_platform.domain.routing import bounded_money
from agent_platform.ports.model import ModelCallFailed


def read_usage(value, request, quote, now, *, decision=False):
    try:
        raw = value["usage"]
        if type(raw) is not dict:
            raise ValueError("usage shape")
        input_tokens = raw["input_tokens" if decision else "prompt_tokens"]
        output_tokens = raw["output_tokens" if decision else "completion_tokens"]
        if type(input_tokens) is not int or type(output_tokens) is not int:
            raise ValueError("token counts")
        if (
            not 0 <= input_tokens <= quote.input_token_upper_bound
            or not 0 <= output_tokens <= quote.max_output_tokens
        ):
            raise ValueError("token counts exceed reservation")
        cost = raw.get("cost")
        if cost is not None:
            if type(cost) not in (str, int, Decimal):
                raise ValueError("cost type")
            cost = Decimal(cost)
            if not cost.is_finite() or cost < 0 or not bounded_money(cost):
                raise ValueError("cost bounds")
        return ModelUsage(
            request_id=request.request_id,
            route_id=request.route.route_id,
            model_version=request.route.model_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=quote.estimated_cost_usd,
            actual_cost_usd=cost,
            billing_status="unknown" if cost is None else "confirmed",
            recorded_at=now,
        )
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise ModelCallFailed("invalid_model_usage") from None


def read_metadata(value, requested_model, usage):
    try:
        actual = value["model"]
        if (
            type(actual) is not str
            or re.fullmatch(re.escape(requested_model) + r"(?:-[0-9]{8})?", actual) is None
        ):
            raise ValueError("unexpected model version")
        return ProviderMetadata(request_id=value["id"], model_id=actual, provider=value["provider"])
    except (ValueError, TypeError, KeyError):
        raise ModelCallFailed("invalid_model_metadata", usage) from None
