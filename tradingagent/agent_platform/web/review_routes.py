"""Protected local review controls with explicit immutable cutoff and confirmation."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from pydantic import AfterValidator, Field

from agent_platform.application.review_queries import group_view, job_view
from agent_platform.domain.common import utc_datetime
from agent_platform.domain.models import Identifier, Revision
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict
from agent_platform.web.feedback_routes import Confirmation


class GroupInput(Confirmation):
    group_id: Annotated[Identifier, Field(max_length=128)]
    cutoff: Annotated[datetime, AfterValidator(utc_datetime)] | None = None


class ReviewInput(Confirmation):
    kind: Literal["initial", "manual", "followup"]
    cutoff: Annotated[datetime, AfterValidator(utc_datetime)]


class FollowupInput(Confirmation):
    hours: tuple[Annotated[int, Field(strict=True)], ...] = Field(max_length=2)


class CancelInput(Confirmation):
    expected_revision: Revision


def register_review_routes(app, guard):
    def service(request, name):
        value = getattr(request.app.state.services, name, None)
        if value is None:
            raise HTTPException(status_code=503, detail="本地复盘服务尚未装配。")
        return value

    async def run(operation):
        try:
            return await operation()
        except (EventIdentityConflict, RequestIdentityConflict, RevisionConflict):
            raise
        except ValueError:
            raise HTTPException(
                status_code=422, detail="复盘条件不满足，请核对成交、截止时间、版本或回访时间。"
            ) from None

    @app.get("/api/review-groups", dependencies=[Depends(guard.require_read)])
    async def groups(
        request: Request,
        after_sequence: int = Query(0, ge=0, le=2**63 - 1),
        limit: int = Query(50, ge=1, le=50),
    ):
        return await run(
            lambda: service(request, "review_queries").groups(
                after_sequence=after_sequence, limit=limit
            )
        )

    @app.get("/api/review-groups/{group_id}/versions", dependencies=[Depends(guard.require_read)])
    async def versions(
        group_id: str,
        request: Request,
        after_revision: int = Query(0, ge=0, le=2**63 - 1),
        limit: int = Query(10, ge=1, le=10),
        trade_offset: int = Query(0, ge=0, le=4096),
    ):
        return await run(
            lambda: service(request, "review_queries").versions(
                group_id, after_revision=after_revision, limit=limit, trade_offset=trade_offset
            )
        )

    @app.post("/api/review-groups", status_code=201, dependencies=[Depends(guard.require_write)])
    async def group(request: Request, body: GroupInput):
        value = service(request, "reviews")

        async def freeze():
            prior = await value.store.review_group(body.group_id, account_ref=value.account_ref)
            cutoff = body.cutoff or (prior.facts.data_cutoff if prior else value.clock.utcnow())
            try:
                frozen = await value.create_group(body.group_id, cutoff)
            except RequestIdentityConflict:
                # A cutoff omitted by the user is assigned once by the server.
                # Only a complete same-scope group can resolve this concurrent retry.
                frozen = await value.store.review_group(
                    body.group_id, account_ref=value.account_ref
                )
                if body.cutoff is not None or frozen is None:
                    raise
            return group_view(frozen)

        return await run(freeze)

    @app.post(
        "/api/review-groups/{group_id}/reviews",
        status_code=201,
        dependencies=[Depends(guard.require_write)],
    )
    async def review(group_id: str, request: Request, body: ReviewInput):
        value = service(request, "reviews")
        return await run(lambda: value.generate(group_id, body.cutoff, body.kind))

    @app.post(
        "/api/review-groups/{group_id}/followups",
        status_code=201,
        dependencies=[Depends(guard.require_write)],
    )
    async def followup(group_id: str, request: Request, body: FollowupInput):
        value = service(request, "review_jobs")
        jobs = await run(lambda: value.schedule_followups(group_id, body.hours))
        return [job_view(job) for job in jobs]

    @app.get("/api/review-jobs", dependencies=[Depends(guard.require_read)])
    async def jobs(request: Request):
        return await run(lambda: service(request, "review_queries").jobs())

    @app.post("/api/review-jobs/{job_id}/cancel", dependencies=[Depends(guard.require_write)])
    async def cancel(job_id: str, request: Request, body: CancelInput):
        value = service(request, "review_jobs")
        return job_view(await run(lambda: value.cancel(job_id, body.expected_revision)))

    @app.get("/api/status", dependencies=[Depends(guard.require_read)])
    async def status(request: Request):
        return await service(request, "system").current()
