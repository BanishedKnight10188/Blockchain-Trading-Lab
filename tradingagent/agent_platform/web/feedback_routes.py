"""Protected local facts with explicit user confirmation and server-owned scope."""

from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import Depends, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, StrictBool, model_validator

from agent_platform.application.ledger_queries import attribution_view, feedback_view, report_view
from agent_platform.domain.attribution import AttributionChange
from agent_platform.domain.common import utc_datetime
from agent_platform.domain.decisions import DecisionFeedback
from agent_platform.domain.ledger_views import MAX_LEDGER_SEQUENCE
from agent_platform.domain.models import Identifier, PositiveAmount, Revision
from agent_platform.domain.reports import UserReportedTrade
from agent_platform.domain.reviews import TradeAttribution
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmed: StrictBool

    @model_validator(mode="after")
    def explicit_confirmation(self) -> Self:
        if not self.confirmed:
            raise ValueError("local facts require explicit confirmation")
        return self


class HumanIdea(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    action: Literal["buy", "sell", "hold"]
    explanation: Identifier
    evidence_ids: tuple[Identifier, ...] = ()
    quantity: PositiveAmount | None = None


class FeedbackInput(Confirmation):
    feedback_id: Identifier
    kind: Literal["accepted", "rejected", "modified", "independent"]
    recommendation_id: Identifier | None = None
    explanation: Identifier
    modified_assessment: HumanIdea | None = None


class AttributionInput(Confirmation):
    operation_id: Identifier
    trade_id: Identifier
    recommendation_id: Identifier | None = None
    expected_revision: Revision
    explanation: Identifier


class ReportInput(Confirmation):
    report_id: Identifier
    side: Literal["buy", "sell"]
    price: PositiveAmount
    quantity: PositiveAmount
    executed_at: Annotated[datetime, AfterValidator(utc_datetime)]
    exchange_trade_id: Identifier | None = None


class VerificationInput(Confirmation):
    operation_id: Identifier
    expected_revision: Revision


def register_feedback_routes(app, guard):
    def service(request, name):
        value = getattr(request.app.state.services, name)
        if value is None:
            raise HTTPException(status_code=503, detail="本地记录服务尚未装配。")
        return value

    async def run(operation):
        try:
            return await operation()
        except (RequestIdentityConflict, EventIdentityConflict, RevisionConflict):
            raise
        except ValueError:
            raise HTTPException(
                status_code=422, detail="记录条件不满足，请核对建议、成交、时间与当前版本。"
            ) from None

    async def commit_with_server_time(value, candidate, lookup, field, recorded_at):
        try:
            return await value.record(candidate)
        except ValueError:
            prior = await lookup()
            if prior is None:
                raise
            # A concurrent commit can also make a later lifecycle precheck fail.
            # Recovery requires a complete receipt; every user field is still compared.
            restored = candidate.model_copy(update={field: recorded_at(prior)})
            return await value.record(restored)

    @app.get("/api/trading-records", dependencies=[Depends(guard.require_read)])
    async def records(
        request: Request,
        after_sequence: int = Query(default=0, ge=0, le=MAX_LEDGER_SEQUENCE),
        limit: int = Query(default=50, ge=1, le=50),
    ):
        return await run(
            lambda: service(request, "ledger").current(after_sequence=after_sequence, limit=limit)
        )

    @app.post("/api/feedback", status_code=201, dependencies=[Depends(guard.require_write)])
    async def feedback(request: Request, body: FeedbackInput):
        value = service(request, "feedback")

        async def record():
            prior = await value.store.feedback(body.feedback_id, account_ref=value.account_ref)
            data = body.model_dump(exclude={"confirmed"})
            if data["modified_assessment"] is not None:
                data["modified_assessment"]["source"] = "human"
            candidate = DecisionFeedback(
                **data, recorded_at=prior.feedback.recorded_at if prior else value.clock.utcnow()
            )
            return await commit_with_server_time(
                value,
                candidate,
                lambda: value.store.feedback(body.feedback_id, account_ref=value.account_ref),
                "recorded_at",
                lambda receipt: receipt.feedback.recorded_at,
            )

        return feedback_view((await run(record)).feedback)

    @app.post("/api/attributions", status_code=201, dependencies=[Depends(guard.require_write)])
    async def attribution(request: Request, body: AttributionInput):
        value = service(request, "attribution")

        async def record():
            prior = await value.store.attribution_change(
                body.operation_id, account_ref=value.account_ref
            )
            original = TradeAttribution(
                account_ref=value.account_ref,
                symbol="BTCUSDT",
                trade_id=body.trade_id,
                recommendation_id=body.recommendation_id,
                original_author="agent" if body.recommendation_id else "human",
                final_decision_maker="human",
                user_confirmed=True,
            )
            candidate = AttributionChange(
                operation_id=body.operation_id,
                attribution=original,
                expected_revision=body.expected_revision,
                explanation=body.explanation,
                recorded_at=prior.recorded_at if prior else value.clock.utcnow(),
            )
            return await commit_with_server_time(
                value,
                candidate,
                lambda: value.store.attribution_change(
                    body.operation_id, account_ref=value.account_ref
                ),
                "recorded_at",
                lambda receipt: receipt.recorded_at,
            )

        receipt = await run(record)
        return attribution_view(receipt.attribution, receipt.revision, receipt.recorded_at)

    @app.post("/api/reports", status_code=201, dependencies=[Depends(guard.require_write)])
    async def report(request: Request, body: ReportInput):
        value = service(request, "reports")

        async def record():
            prior = await value.store.report(body.report_id, account_ref=value.account_ref)
            candidate = UserReportedTrade(
                **body.model_dump(exclude={"confirmed"}),
                account_ref=value.account_ref,
                reported_at=prior.state.report.reported_at if prior else value.clock.utcnow(),
            )
            return await commit_with_server_time(
                value,
                candidate,
                lambda: value.store.report(body.report_id, account_ref=value.account_ref),
                "reported_at",
                lambda receipt: receipt.state.report.reported_at,
            )

        return report_view(await run(record))

    @app.post("/api/reports/{report_id}/verify", dependencies=[Depends(guard.require_write)])
    async def verify(report_id: str, request: Request, body: VerificationInput):
        value = service(request, "reports")
        receipt = await run(
            lambda: value.verify(report_id, body.operation_id, body.expected_revision)
        )
        return report_view(receipt)
