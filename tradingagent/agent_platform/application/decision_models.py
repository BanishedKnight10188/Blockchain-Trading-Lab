"""Durable budget gate for typed inference; never publishes a trade recommendation."""

import asyncio
import json
import re
from contextlib import contextmanager
from decimal import Context, Decimal, localcontext
from time import perf_counter_ns

from agent_platform.domain.common import nonnegative_amount
from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.domain.decision_models import DecisionModelRequest, DecisionModelResponse
from agent_platform.domain.model_diagnostics import ModelDiagnostic, ModelLocalTiming
from agent_platform.domain.model_modules import JevModuleSettings
from agent_platform.domain.routing import ModelCostQuote, ModelPrice, bounded_money
from agent_platform.ports.model import ModelCallFailed

from .settlement import settle_owned


class _CallTiming:
    def __init__(self):
        self.began = perf_counter_ns()
        self.phases = dict.fromkeys(
            ("validation", "reserve", "provider", "fee_validation", "settle", "binding"), 0
        )

    @contextmanager
    def phase(self, name):
        began = perf_counter_ns()
        try:
            yield
        finally:
            self.phases[name] += perf_counter_ns() - began

    def evidence(self):
        return ModelLocalTiming(
            **{name + "_us": value // 1000 for name, value in self.phases.items()},
            total_us=(perf_counter_ns() - self.began) // 1000,
        )


class BudgetedDecisionModel:
    # Only the provider wait is timed. Durable fee settlement owns its IO until
    # completion, after which an expired decision is rejected without execution.
    provider_deadline_managed = True

    def __init__(
        self,
        *,
        port,
        budgets,
        clock,
        price: ModelPrice,
        daily_limit_usd="0",
        max_single_cost_usd="0",
        hourly_call_limit=60,
        settings: JevModuleSettings | None = None,
        provider_managed=False,
    ):
        self.port, self.budgets, self.clock = port, budgets, clock
        if type(provider_managed) is not bool:
            raise ValueError("provider-managed mode must be explicit")
        self.provider_managed = provider_managed
        self.price = ModelPrice.model_validate_json(price.model_dump_json())
        self.daily_limit_usd, self.max_single_cost_usd = (
            nonnegative_amount(daily_limit_usd),
            nonnegative_amount(max_single_cost_usd),
        )
        if (
            any(
                not value.is_finite() or value < 0 or not bounded_money(value)
                for value in (self.daily_limit_usd, self.max_single_cost_usd)
            )
            or type(hourly_call_limit) is not int
            or not 0 <= hourly_call_limit <= 3600
        ):
            raise ValueError("invalid typed inference budget")
        self.hourly_call_limit = hourly_call_limit
        self._settings = JevModuleSettings.model_validate_json(
            (settings or JevModuleSettings()).model_dump_json()
        )
        self._activation_revision = 1

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def set_enabled(self, value: bool) -> None:
        settings = JevModuleSettings(enabled=value)
        if settings != self._settings:
            self._settings = settings
            self._activation_revision += 1

    def _active(self, revision: int) -> bool:
        return self.enabled and revision == self._activation_revision

    def quote(self, request):
        now = self.clock.utcnow()
        if request.route.price_version != self.price.version or not self.price.available_at(now):
            raise ValueError("typed model price is unavailable")
        bound = len(request.model_dump_json().encode()) + 1024
        if bound + request.max_output_tokens > 32000:
            raise ValueError("typed request exceeds conservative context bound")
        with localcontext(Context(prec=128)):
            cost = (
                Decimal(bound) * self.price.input_usd_per_million
                + Decimal(request.max_output_tokens) * self.price.output_usd_per_million
            ) / Decimal(1000000)
        if not self.provider_managed and cost > self.max_single_cost_usd:
            raise ValueError("typed request exceeds single-call ceiling")
        return ModelCostQuote(
            route_id=request.route.route_id,
            price_version=self.price.version,
            input_token_upper_bound=bound,
            max_output_tokens=request.max_output_tokens,
            estimated_cost_usd=cost,
        )

    def _validate_fee(self, request, usage, quote, *, not_before=None):
        if not isinstance(usage, ModelUsage):
            raise ValueError("typed fee evidence is required")
        usage = ModelUsage.model_validate_json(usage.model_dump_json())
        if any(
            not bounded_money(value)
            for value in (usage.estimated_cost_usd, usage.actual_cost_usd)
            if value is not None
        ):
            raise ValueError("typed fee arithmetic exceeds bounds")
        earliest = (
            max(request.captured_at, not_before) if not_before is not None else request.captured_at
        )
        if (
            usage.request_id != request.request_id
            or usage.route_id != request.route.route_id
            or usage.model_version != request.route.model_version
            or usage.estimated_cost_usd != quote.estimated_cost_usd
            or usage.input_tokens > quote.input_token_upper_bound
            or usage.output_tokens > quote.max_output_tokens
            or not earliest <= usage.recorded_at <= self.clock.utcnow()
        ):
            raise ValueError("typed fee evidence does not match reservation")
        return usage

    async def _unknown(self, request, quote, reservation, timing):
        usage = ModelUsage(
            request_id=request.request_id,
            route_id=request.route.route_id,
            model_version=request.route.model_version,
            input_tokens=0,
            output_tokens=0,
            token_counts_known=False,
            estimated_cost_usd=quote.estimated_cost_usd,
            billing_status="unknown",
            recorded_at=self.clock.utcnow(),
        )
        with timing.phase("settle"):
            await settle_owned(self.budgets, reservation.reservation_id, usage)
        return usage

    async def decide(self, request: DecisionModelRequest) -> DecisionModelResponse:
        timing = _CallTiming()
        try:
            response = await self._decide(request, timing)
        except ModelCallFailed as error:
            diagnostic = error.diagnostic or ModelDiagnostic(stage="binding")
            diagnostic = diagnostic.model_copy(update={"local_timing": timing.evidence()})
            raise ModelCallFailed(error.reason, error.usage, diagnostic) from None
        return response.model_copy(update={"local_timing": timing.evidence()})

    async def _decide(self, request, timing):
        with timing.phase("validation"):
            request = DecisionModelRequest.model_validate_json(request.model_dump_json())
        if (
            not self.enabled
            or self.port is None
            or self.clock.utcnow() >= request.deadline
            or self.clock.utcnow() < request.captured_at
        ):
            raise ModelCallFailed("model_module_disabled")
        revision = self._activation_revision
        with timing.phase("validation"):
            quote = self.quote(request)
        with timing.phase("reserve"):
            state = json.loads(request.state_json)
            account = state.get("account")
            scope = account.get("scope", {}) if isinstance(account, dict) else {}
            reservation = await self.budgets.reserve(
                BudgetRequest(
                    request_id=request.request_id,
                    route_id=request.route.route_id,
                    purpose=request.route.purpose,
                    price_version=quote.price_version,
                    estimated_cost_usd=quote.estimated_cost_usd,
                    daily_limit_usd=self.daily_limit_usd,
                    hourly_call_limit=self.hourly_call_limit,
                    requested_at=self.clock.utcnow(),
                    provider_managed=self.provider_managed,
                    session_id=scope.get("session_id") if isinstance(scope, dict) else None,
                ),
                require_new=True,
            )
        if (
            not self._active(revision)
            or self.clock.utcnow() >= request.deadline
            or not self.price.available_at(self.clock.utcnow())
        ):
            usage = ModelUsage(
                request_id=request.request_id,
                route_id=request.route.route_id,
                model_version=request.route.model_version,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=quote.estimated_cost_usd,
                actual_cost_usd="0",
                billing_status="confirmed",
                recorded_at=self.clock.utcnow(),
            )
            with timing.phase("settle"):
                await settle_owned(self.budgets, reservation.reservation_id, usage)
            reason = "quote_unavailable" if self._active(revision) else "model_module_disabled"
            raise ModelCallFailed(reason, usage)
        try:
            with timing.phase("provider"):
                # One timer owns native transport and its socket cleanup/trace.
                # A second timer can cancel that cleanup and erase diagnostics.
                response_deadline = request.response_deadline or request.deadline
                timeout = (
                    None
                    if getattr(self.port, "provider_deadline_managed", False) is True
                    else min(15, (response_deadline - self.clock.utcnow()).total_seconds())
                )
                async with asyncio.timeout(timeout):
                    response = await self.port.decide(request, quote)
        except asyncio.CancelledError:
            await self._unknown(request, quote, reservation, timing)
            raise
        except ModelCallFailed as error:
            try:
                with timing.phase("fee_validation"):
                    usage = self._validate_fee(
                        request, error.usage, quote, not_before=reservation.request.requested_at
                    )
            except (ValueError, TypeError, AttributeError):
                usage = await self._unknown(request, quote, reservation, timing)
            else:
                with timing.phase("settle"):
                    await settle_owned(self.budgets, reservation.reservation_id, usage)
            raise ModelCallFailed(error.reason, usage, error.diagnostic) from None
        except TimeoutError:
            usage = await self._unknown(request, quote, reservation, timing)
            raise ModelCallFailed(
                "provider_timeout", usage, ModelDiagnostic(stage="transport")
            ) from None
        except Exception:
            usage = await self._unknown(request, quote, reservation, timing)
            raise ModelCallFailed("provider_error", usage) from None
        try:
            with timing.phase("fee_validation"):
                if not isinstance(response, DecisionModelResponse):
                    raise ValueError("typed response required")
                usage = self._validate_fee(
                    request, response.usage, quote, not_before=reservation.request.requested_at
                )
        except (ValueError, TypeError, AttributeError):
            usage = await self._unknown(request, quote, reservation, timing)
            raise ModelCallFailed("invalid_model_usage", usage) from None
        with timing.phase("settle"):
            await settle_owned(self.budgets, reservation.reservation_id, usage)
        if not self._active(revision):
            raise ModelCallFailed("model_module_disabled", usage)
        try:
            with timing.phase("binding"):
                response = DecisionModelResponse.model_validate_json(response.model_dump_json())
                response.bind_to(request)
                if (
                    not self.price.available_at(self.clock.utcnow())
                    or re.fullmatch(
                        re.escape(request.route.model_version) + r"(?:-[0-9]{8})?",
                        response.provider_metadata.model_id,
                    )
                    is None
                ):
                    raise ValueError("typed response is no longer eligible")
        except (ValueError, TypeError, AttributeError):
            raise ModelCallFailed(
                "invalid_model_assessment", usage, ModelDiagnostic(stage="binding")
            ) from None
        if self.clock.utcnow() >= request.deadline:
            # A bound response with confirmed fees can expire during settlement.
            # Reject that verdict without misclassifying it as malformed.
            raise ModelCallFailed(
                "decision_expired",
                usage,
                ModelDiagnostic(stage="binding", transport_evidence=response.transport_evidence),
            )
        return response
