"""Historical publication is never sufficient to display advice as current."""

from agent_platform.application.risk import RiskService
from agent_platform.application.snapshots import SnapshotFactory
from agent_platform.domain.advice_views import AdviceView, UsageView
from agent_platform.domain.overview import OverviewFrame
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.decisions import DecisionReadPort
from agent_platform.ports.sessions import SessionStorePort


class AdviceQueryService:
    def __init__(
        self,
        sessions: SessionStorePort,
        decisions: DecisionReadPort,
        snapshots: SnapshotFactory,
        clock: ClockPort,
    ):
        self.sessions, self.decisions, self.snapshots, self.clock = (
            sessions,
            decisions,
            snapshots,
            clock,
        )
        self.risk = RiskService(clock)

    async def current(self, frame: OverviewFrame | None = None) -> AdviceView:
        session = await self.sessions.active()
        if session is None:
            return AdviceView(status="unavailable", reasons=("no_session",))
        record = await self.decisions.latest_decision(session.session_id)
        if record is None:
            return AdviceView(status="unavailable", reasons=("no_decision",))
        request, completion = record.request, record.completion
        original = request.snapshot
        base = dict(
            style_strength=original.style.strength,
            style_revision=original.style_revision,
            policy_version=original.style.policy_version,
            evidence_as_of=original.captured_at,
        )
        changed = (
            session.status != "running"
            or session.revision != original.session_revision
            or session.style_revision != original.style_revision
            or session.style != original.style
        )
        if completion is None:
            if changed:
                return AdviceView(status="superseded", reasons=("session_changed",), **base)
            if self.clock.utcnow() >= request.deadline:
                return AdviceView(status="unavailable", reasons=("unfinished_request",), **base)
            return AdviceView(status="pending", expires_at=request.deadline, **base)
        result, advice = completion.result, completion.result.recommendation
        usage = result.usage
        base.update(
            model_failure=completion.model_failure,
            usage_status="recorded" if usage else "not_recorded",
            usage=UsageView(
                billing_status=usage.billing_status,
                estimated_cost_usd=usage.estimated_cost_usd,
                actual_cost_usd=usage.actual_cost_usd,
                token_counts_known=usage.token_counts_known,
                input_tokens=usage.input_tokens if usage.token_counts_known else None,
                output_tokens=usage.output_tokens if usage.token_counts_known else None,
            )
            if usage
            else None,
        )
        if advice is None:
            return AdviceView(status=result.status, reasons=result.reasons, **base)
        base.update(
            recommendation_id=advice.recommendation_id,
            original_author=advice.original_author,
            source=advice.assessment.source,
            created_at=advice.created_at,
            expires_at=advice.expires_at,
        )
        if result.status != "published":
            return AdviceView(status=result.status, reasons=result.reasons, **base)
        if record.current_recommendation is None:
            return AdviceView(
                status="unavailable", reasons=("recommendation_state_unavailable",), **base
            )
        if record.current_recommendation.status != "published":
            return AdviceView(
                status=record.current_recommendation.status, reasons=("feedback_recorded",), **base
            )
        if changed:
            return AdviceView(status="superseded", reasons=("session_changed",), **base)
        if self.clock.utcnow() >= advice.expires_at:
            return AdviceView(status="expired", reasons=("advice_expired",), **base)
        account = await self.snapshots.account()
        if account is not None and (
            account.account_ref != original.account.account_ref
            or account.market_type != original.account.market_type
            or account.account_revision != original.account.account_revision
            or account.balances != original.account.balances
        ):
            return AdviceView(status="superseded", reasons=("account_changed",), **base)
        current = await self.snapshots.for_trigger(
            original.trigger,
            limits=original.limits,
            retained_evidence=original.evidence_ids,
            frame=frame,
        )
        if current is None:
            return AdviceView(
                status="unavailable", reasons=("current_evidence_unavailable",), **base
            )
        if (
            current.session_revision != original.session_revision
            or current.style_revision != original.style_revision
            or current.style != original.style
        ):
            return AdviceView(status="superseded", reasons=("session_changed",), **base)
        if (
            current.account.account_ref != original.account.account_ref
            or current.account.market_type != original.account.market_type
            or current.account.account_revision != original.account.account_revision
            or current.account.balances != original.account.balances
        ):
            return AdviceView(status="superseded", reasons=("account_changed",), **base)
        latest = await self.decisions.latest_decision(session.session_id)
        if latest is None or latest.request != request:
            return AdviceView(status="superseded", reasons=("decision_changed",), **base)
        if latest.current_recommendation is None:
            return AdviceView(
                status="unavailable", reasons=("recommendation_state_unavailable",), **base
            )
        if latest.current_recommendation.status != "published":
            return AdviceView(
                status=latest.current_recommendation.status, reasons=("feedback_recorded",), **base
            )
        if self.clock.utcnow() >= advice.expires_at:
            return AdviceView(status="expired", reasons=("advice_expired",), **base)
        risk = self.risk.evaluate(current, advice.assessment, context=completion.risk_context)
        if risk.outcome != "allow":
            return AdviceView(status="unavailable", reasons=risk.reasons, **base)
        return AdviceView(
            status="published",
            action=advice.assessment.action,
            explanation=advice.assessment.explanation,
            quantity=advice.assessment.quantity,
            **base,
        )
