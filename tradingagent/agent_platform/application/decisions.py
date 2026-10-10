"""Single durable attempt, conservative billing, free rule fallback and current risk."""

import asyncio
from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.common import DecisionPurpose
from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.domain.decision_requests import DecisionCompletion, DecisionRequest
from agent_platform.domain.decisions import (
    AdvisoryAssessment,
    DecisionResult,
    DecisionSnapshot,
    Recommendation,
)
from agent_platform.domain.model_calls import ModelRequest, ModelResponse
from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.domain.risk import RiskAssessment, RiskContext
from agent_platform.ports.advisory import AdvisoryPort
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.decisions import DecisionStorePort, PublicationEvidencePort
from agent_platform.ports.model import ModelCallFailed, ModelPort
from agent_platform.ports.persistence import (
    BudgetExceeded,
    BudgetFrozen,
    BudgetStorePort,
    DispatchAlreadyReserved,
    HourlyCallLimitExceeded,
)

from .prompting import (
    PROMPT_VERSION,
    build_prompt,
    validate_assessment,
    validate_response,
    validate_usage,
)
from .risk import RiskService
from .routing import ModelRouter
from .settlement import settle_owned


class DecisionService:
    def __init__(
        self,
        *,
        clock: ClockPort,
        router: ModelRouter,
        decisions: DecisionStorePort,
        budgets: BudgetStorePort,
        current: PublicationEvidencePort,
        model: ModelPort | None = None,
        rules: AdvisoryPort | None = None,
        risk_context: RiskContext | None = None,
        model_modules: ModelModulesConfig | None = None,
    ):
        self.clock, self.router, self.decisions = clock, router, decisions
        self.budgets, self.current, self.model, self.rules = budgets, current, model, rules
        self.risk_context = risk_context or RiskContext()
        self.risk = RiskService(clock)
        self.model_modules = ModelModulesConfig.model_validate_json(
            (model_modules or ModelModulesConfig()).model_dump_json()
        )

    def prepare(
        self,
        snapshot: DecisionSnapshot,
        *,
        request_id: str,
        purpose: DecisionPurpose | str = "advisory",
    ) -> DecisionRequest:
        now = self.clock.utcnow()
        return DecisionRequest(
            request_id=request_id,
            snapshot=snapshot,
            route=self.router.select(snapshot, purpose),
            requested_at=now,
            deadline=min(now + timedelta(seconds=15), snapshot.trigger.expires_at),
            prompt_version=PROMPT_VERSION,
        )

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        receipt = await self.decisions.claim(request)
        if receipt.result is not None:
            return receipt.result
        if not receipt.claimed:
            return self._missing(request, ("request_in_flight",))
        request = receipt.request
        if self.clock.utcnow() >= request.deadline:
            return await self._unavailable(request, ("request_expired",))
        probe = AdvisoryAssessment(action="hold", explanation="数据和纪律预检", source="rule")
        risk = self.risk.evaluate(request.snapshot, probe, context=self.risk_context)
        if not risk.permits_advice:
            return await self._unavailable(request, risk.reasons, risk=risk)
        if request.route != self.router.select(request.snapshot, request.route.purpose):
            return await self._unavailable(request, ("route_not_current",))
        if request.route.kind == "rule":
            return await self._rule(request)
        if self.model is None:
            return await self._rule(request, failure="model_unconfigured")
        if request.route.model_version == self.model_modules.strong_model.model_id:
            return await self._rule(request, failure="model_module_disabled")
        tier = next(item for item in self.router.policy.routes if item.kind == request.route.kind)
        model_request = ModelRequest(
            request_id=request.request_id,
            snapshot=request.snapshot,
            purpose=request.route.purpose,
            route=request.route,
            deadline=request.deadline,
            max_output_tokens=tier.max_output_tokens,
            prompt_version=request.prompt_version,
        )
        try:
            check = getattr(self.model, "check_request", None)
            if check is not None:
                check(model_request)
        except (ModelCallFailed, ValueError, TypeError):
            return await self._rule(request, failure="model_module_disabled")
        try:
            quote = self.router.quote(request.route, build_prompt(model_request))
        except ValueError:
            return await self._rule(request, failure="quote_unavailable")
        try:
            reservation = await self.budgets.reserve(
                BudgetRequest(
                    request_id=request.request_id,
                    route_id=request.route.route_id,
                    purpose=request.route.purpose,
                    price_version=quote.price_version,
                    estimated_cost_usd=quote.estimated_cost_usd,
                    daily_limit_usd=self.router.policy.daily_limit_usd,
                    hourly_call_limit=self.router.policy.hourly_call_limit,
                    requested_at=self.clock.utcnow(),
                ),
                require_new=True,
            )
        except DispatchAlreadyReserved:
            return await self._rule(request, failure="prior_budget_attempt")
        except (BudgetExceeded, BudgetFrozen, HourlyCallLimitExceeded):
            return await self._rule(request, failure="budget_unavailable")
        remaining = (request.deadline - self.clock.utcnow()).total_seconds()
        if remaining <= 0:
            return await self._no_dispatch(
                request,
                quote,
                reservation,
                "request_expired",
                "expired_before_dispatch",
            )
        if request.route != self.router.select(request.snapshot, request.route.purpose):
            return await self._no_dispatch(
                request,
                quote,
                reservation,
                "route_not_current",
                "price_expired_before_dispatch",
            )
        try:
            async with asyncio.timeout(min(15, remaining)):
                response = await self.model.generate(model_request)
        except asyncio.CancelledError:
            usage = await self._unknown(model_request, quote, reservation)
            await self._unavailable(request, ("provider_cancelled",), usage=usage)
            raise
        except TimeoutError:
            usage = await self._unknown(model_request, quote, reservation)
            return await self._rule(request, usage=usage, failure="provider_timeout")
        except ModelCallFailed as error:
            try:
                if error.usage is None:
                    raise ValueError("no fee evidence")
                usage = validate_usage(
                    model_request,
                    error.usage,
                    quote,
                    self.clock.utcnow(),
                    not_before=reservation.request.requested_at,
                )
            except (ValueError, TypeError, AttributeError):
                usage = await self._unknown(model_request, quote, reservation)
            else:
                await settle_owned(self.budgets, reservation.reservation_id, usage)
            return await self._rule(request, usage=usage, failure=error.reason)
        except Exception:
            usage = await self._unknown(model_request, quote, reservation)
            return await self._rule(request, usage=usage, failure="provider_error")
        try:
            if not isinstance(response, ModelResponse):
                raise ValueError("model must return owned response")
            usage = validate_usage(
                model_request,
                response.usage,
                quote,
                self.clock.utcnow(),
                not_before=reservation.request.requested_at,
            )
        except (ValueError, TypeError, AttributeError):
            usage = await self._unknown(model_request, quote, reservation)
            return await self._rule(request, usage=usage, failure="invalid_model_usage")
        await settle_owned(self.budgets, reservation.reservation_id, usage)
        try:
            validate_response(model_request, response, quote, self.clock.utcnow())
        except (ValueError, TypeError, AttributeError):
            return await self._rule(request, usage=usage, failure="invalid_model_assessment")
        return await self._publish(request, response.assessment, usage=usage, response=response)

    async def _no_dispatch(self, request, quote, reservation, reason, failure):
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
        await settle_owned(self.budgets, reservation.reservation_id, usage)
        return await self._unavailable(request, (reason,), usage=usage, failure=failure)

    async def _unknown(self, request, quote, reservation):
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
        await settle_owned(self.budgets, reservation.reservation_id, usage)
        return usage

    async def _rule(self, request, *, usage=None, failure=None):
        if self.rules is None:
            return await self._unavailable(
                request,
                (failure or "rule_unconfigured",),
                usage=usage,
                failure=failure,
            )
        if self.clock.utcnow() >= request.deadline:
            return await self._unavailable(
                request,
                (failure or "request_expired",),
                usage=usage,
                failure=failure,
            )
        try:
            assessment = self.rules.evaluate(request.snapshot)
            if not isinstance(assessment, AdvisoryAssessment) or assessment.source != "rule":
                raise ValueError("rule must retain its original author")
            validate_assessment(assessment, request)
        except (ValueError, TypeError, AttributeError):
            return await self._unavailable(request, ("invalid_rule",), usage=usage, failure=failure)
        return await self._publish(request, assessment, usage=usage, failure=failure)

    async def _publish(self, request, assessment, *, usage=None, response=None, failure=None):
        now = self.clock.utcnow()
        expires = min(
            request.snapshot.captured_at + timedelta(seconds=assessment.valid_for_seconds),
            request.snapshot.trigger.expires_at,
        )
        if now >= expires or now >= request.deadline:
            return await self._unavailable(
                request,
                ("advice_expired",),
                usage=usage,
                response=response,
                failure=failure,
            )
        if assessment.action == "unavailable":
            return await self._unavailable(
                request,
                assessment.unavailable_reasons,
                usage=usage,
                response=response,
                failure=failure,
            )
        publication = await self.current.capture(request)
        if publication is None:
            return await self._unavailable(
                request,
                ("current_evidence_unavailable",),
                usage=usage,
                response=response,
                failure=failure,
            )
        original = request.snapshot
        if not isinstance(publication, DecisionSnapshot) or (
            publication.captured_at > self.clock.utcnow()
            or publication.snapshot_id == original.snapshot_id
            or publication.session_id != original.session_id
            or publication.account.account_ref != original.account.account_ref
            or publication.account.market_type != original.account.market_type
            or publication.market.symbol != original.market.symbol
            or publication.limits != original.limits
            or publication.trigger != original.trigger
        ):
            return await self._unavailable(
                request,
                ("current_evidence_invalid",),
                usage=usage,
                response=response,
                failure=failure,
            )
        risk = self.risk.evaluate(publication, assessment, context=self.risk_context)
        if not risk.permits_advice:
            return await self._unavailable(
                request,
                risk.reasons,
                usage=usage,
                response=response,
                failure=failure,
                risk=risk,
                publication=publication,
            )
        now = self.clock.utcnow()
        if now >= expires:
            return await self._unavailable(
                request, ("advice_expired",), usage=usage, response=response
            )
        original = request.snapshot
        recommendation = Recommendation(
            recommendation_id="advice:" + sha256(request.request_id.encode()).hexdigest(),
            snapshot_id=original.snapshot_id,
            session_id=original.session_id,
            style_revision=original.style_revision,
            account_revision=original.account.account_revision,
            assessment=assessment,
            created_at=now,
            updated_at=now,
            expires_at=expires,
        ).transition("published", now)
        result = DecisionResult(
            request_id=request.request_id,
            snapshot_id=original.snapshot_id,
            publication_snapshot_id=publication.snapshot_id,
            status="published",
            recommendation=recommendation,
            risk=risk,
            usage=usage,
        )
        return await self.decisions.finish(
            request.request_id,
            DecisionCompletion(
                result=result,
                completed_at=now,
                publication_snapshot=publication,
                risk_context=self.risk_context,
                response=response,
                model_failure=failure,
            ),
        )

    def _missing(self, request, reasons, *, usage=None, risk=None, publication=None):
        risk = risk or RiskAssessment(
            snapshot_id=request.snapshot.snapshot_id,
            outcome="unavailable",
            reasons=reasons,
            evaluated_at=self.clock.utcnow(),
        )
        return DecisionResult(
            request_id=request.request_id,
            snapshot_id=request.snapshot.snapshot_id,
            publication_snapshot_id=publication.snapshot_id if publication is not None else None,
            status="unavailable",
            risk=risk,
            usage=usage,
            reasons=reasons,
        )

    async def _unavailable(
        self,
        request,
        reasons,
        *,
        usage=None,
        risk=None,
        publication=None,
        response=None,
        failure=None,
    ):
        result = self._missing(request, reasons, usage=usage, risk=risk, publication=publication)
        return await self.decisions.finish(
            request.request_id,
            DecisionCompletion(
                result=result,
                completed_at=self.clock.utcnow(),
                publication_snapshot=publication,
                risk_context=self.risk_context,
                response=response,
                model_failure=failure,
            ),
        )
