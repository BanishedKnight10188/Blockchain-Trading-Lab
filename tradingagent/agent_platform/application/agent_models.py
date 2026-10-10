"""Separate grant gate on the existing cumulative fee journal."""

import asyncio

from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.domain.event_agent import AgentBudgetGrant, AgentTurnRequest
from agent_platform.domain.routing import ModelCostQuote, ModelPrice
from agent_platform.ports.model import ModelCallFailed


class BudgetedAgentModel:
    paid = True

    def __init__(self, port, budgets, clock, *, price: ModelPrice, grant: AgentBudgetGrant | None):
        self.port, self.budgets, self.clock = port, budgets, clock
        self.price = ModelPrice.model_validate_json(price.model_dump_json())
        self.grant = (
            AgentBudgetGrant.model_validate_json(grant.model_dump_json()) if grant else None
        )

    def quote(self, request):
        if hasattr(self.port, "quote"):
            return self.port.quote(request)
        from decimal import Context, Decimal, localcontext

        size = len(request.model_dump_json().encode()) + 4096
        with localcontext(Context(prec=80)):
            estimate = (
                self.price.input_usd_per_million * size
                + self.price.output_usd_per_million * request.max_output_tokens
            ) / Decimal(1000000)
        return ModelCostQuote(
            route_id=request.route.route_id,
            price_version=self.price.version,
            input_token_upper_bound=size,
            max_output_tokens=request.max_output_tokens,
            estimated_cost_usd=estimate,
        )

    async def turn(self, request):
        request = AgentTurnRequest.model_validate_json(request.model_dump_json())
        now, grant = self.clock.utcnow(), self.grant
        if (
            grant is None
            or grant.lane_id != request.lane_id
            or grant.model_id != request.route.model_version
            or not request.route.paid
            or grant.price_version != self.price.version
            or request.route.price_version != self.price.version
            or not grant.valid_from <= now < grant.expires_at
            or not self.price.available_at(now)
            or now >= request.deadline
        ):
            raise ValueError("new event-agent budget grant is required")
        quote = self.quote(request)
        reservation = await self.budgets.reserve_agent(
            BudgetRequest(
                request_id=request.request_id,
                route_id=request.route.route_id,
                purpose=request.route.purpose,
                price_version=quote.price_version,
                estimated_cost_usd=quote.estimated_cost_usd,
                daily_limit_usd=grant.parent_total_usd,
                hourly_call_limit=grant.hourly_call_limit,
                requested_at=now,
            ),
            grant,
        )
        usage = None
        try:
            async with asyncio.timeout((request.deadline - now).total_seconds()):
                response = await self.port.turn(request)
            usage = response.usage
            if (
                usage.request_id != request.request_id
                or usage.route_id != request.route.route_id
                or usage.model_version != request.route.model_version
                or usage.estimated_cost_usd != quote.estimated_cost_usd
            ):
                usage = None
                raise ModelCallFailed("agent_usage_binding_failed")
            return response
        except ModelCallFailed as error:
            candidate = error.usage
            if candidate is not None and (
                candidate.request_id,
                candidate.route_id,
                candidate.model_version,
                candidate.estimated_cost_usd,
            ) == (
                request.request_id,
                request.route.route_id,
                request.route.model_version,
                quote.estimated_cost_usd,
            ):
                usage = candidate
            raise
        finally:
            if usage is None:
                usage = ModelUsage(
                    request_id=request.request_id,
                    route_id=request.route.route_id,
                    model_version=request.route.model_version,
                    input_tokens=0,
                    output_tokens=0,
                    token_counts_known=False,
                    estimated_cost_usd=quote.estimated_cost_usd,
                    actual_cost_usd=None,
                    billing_status="unknown",
                    recorded_at=self.clock.utcnow(),
                )
            # owned settlement continues to completion even during cancellation.
            from .settlement import settle_owned

            await settle_owned(self.budgets, reservation.reservation_id, usage)
