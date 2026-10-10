"""Deterministic tier choice and exact request ceilings; never invokes models."""

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from hashlib import sha256

from agent_platform.domain.common import DecisionPurpose
from agent_platform.domain.costs import RouteDecision
from agent_platform.domain.decisions import DecisionSnapshot
from agent_platform.domain.routing import ModelCostQuote, ModelTier, RoutingPolicy
from agent_platform.ports.clock import ClockPort


class ModelRouter:
    def __init__(self, policy: RoutingPolicy, clock: ClockPort):
        self.policy = RoutingPolicy.model_validate_json(policy.model_dump_json())
        self.clock = clock

    def _eligible(self, tier: ModelTier) -> bool:
        price = tier.price
        return (
            tier.enabled
            and self.policy.daily_limit_usd > 0
            and self.policy.hourly_call_limit > 0
            and price is not None
            and price.available_at(self.clock.utcnow())
        )

    def _route(self, tier: ModelTier, purpose: DecisionPurpose) -> RouteDecision:
        identity = sha256(
            (self.policy.model_dump_json() + ":" + purpose.value + ":" + tier.kind).encode()
        ).hexdigest()
        return RouteDecision(
            route_id="route:" + identity,
            kind=tier.kind,
            purpose=purpose,
            reason="configured_" + tier.kind,
            model_version=tier.model_version,
            price_version=tier.price.version,
            paid=True,
        )

    def select(self, snapshot: DecisionSnapshot, purpose: DecisionPurpose | str) -> RouteDecision:
        purpose = DecisionPurpose(purpose)
        kind = (
            "review"
            if purpose == DecisionPurpose.REVIEW
            else (
                "standard"
                if snapshot.trigger.kind == "account_change" and snapshot.position.quantity > 0
                else "economy"
            )
        )
        tier = next((item for item in self.policy.routes if item.kind == kind), None)
        if tier is not None and self._eligible(tier):
            return self._route(tier, purpose)
        return RouteDecision(
            route_id="rule:" + purpose.value,
            kind="rule",
            purpose=purpose,
            reason="configured_model_unavailable",
        )

    def quote(self, route: RouteDecision, prompt: bytes) -> ModelCostQuote:
        if type(prompt) is not bytes or not 1 <= len(prompt) <= 65536:
            raise ValueError("prompt must have a known bounded byte size")
        tier = next((item for item in self.policy.routes if item.kind == route.kind), None)
        if tier is None or not self._eligible(tier) or route != self._route(tier, route.purpose):
            raise ValueError("route is not an eligible configured model")
        input_bound = len(prompt) + tier.prompt_overhead_tokens
        price = tier.price
        with localcontext(
            Context(prec=512, Emin=-1024, Emax=1024, rounding=ROUND_HALF_EVEN, clamp=0)
        ):
            estimate = (
                Decimal(input_bound) * price.input_usd_per_million
                + Decimal(tier.max_output_tokens) * price.output_usd_per_million
            ) / Decimal(1000000)
        if estimate > tier.max_single_cost_usd or estimate > self.policy.daily_limit_usd:
            raise ValueError("request exceeds configured cost ceiling")
        return ModelCostQuote(
            route_id=route.route_id,
            price_version=price.version,
            input_token_upper_bound=input_bound,
            max_output_tokens=tier.max_output_tokens,
            estimated_cost_usd=estimate,
        )
