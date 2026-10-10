"""Bounded browser projections of complete local review evidence."""

from agent_platform.domain.common import live_account_ref
from agent_platform.ports.overview import OverviewPort
from agent_platform.ports.review_jobs import ReviewJobStorePort


def group_view(group):
    return dict(
        group_id=group.group.trade_group_id,
        trade_count=len(group.group.trades),
        cutoff=group.facts.data_cutoff.isoformat(),
        registered_at=group.registered_at.isoformat(),
        last_execution_at=group.group.trades[-1].executed_at.isoformat(),
        cost_status=group.facts.cost_status.value,
    )


def job_view(job):
    return dict(
        job_id=job.job_id,
        group_id=job.trade_group_id,
        kind=job.kind.value,
        created_at=job.created_at.isoformat(),
        scheduled_for=job.scheduled_for.isoformat(),
        status=job.status.value,
        review_id=job.review_id,
        failure_reason=job.failure_reason,
        revision=1 if job.status == "pending" else 2,
    )


def check_view(check):
    result = check.model_dump(
        mode="json",
        exclude={
            "original_request_id",
            "original_snapshot_id",
            "publication_snapshot_id",
            "original_model_usage",
            "publication_risk",
        },
    )
    result["publication_risk"] = (
        check.publication_risk.model_dump(mode="json", exclude={"snapshot_id"})
        if check.publication_risk
        else None
    )
    usage = check.original_model_usage
    result["original_model_usage"] = (
        usage.model_dump(
            mode="json",
            include={
                "billing_status",
                "estimated_cost_usd",
                "actual_cost_usd",
                "token_counts_known",
                "input_tokens",
                "output_tokens",
                "recorded_at",
            },
        )
        if usage
        else None
    )
    if usage and not usage.token_counts_known:
        result["original_model_usage"].update(input_tokens=None, output_tokens=None)
    return result


class ReviewQueryService:
    def __init__(self, *, store: ReviewJobStorePort, source: OverviewPort, account_ref: str):
        self.store, self.source, self.account_ref = store, source, live_account_ref(account_ref)

    async def groups(self, *, after_sequence=0, limit=50):
        page = await self.store.review_groups(
            self.account_ref, after_sequence=after_sequence, limit=limit
        )
        return dict(
            mode=(await self.source.latest()).mode,
            groups=[group_view(group) for group in page.groups],
            next_sequence=page.next_sequence,
            has_more=page.has_more,
        )

    async def versions(self, group_id, *, after_revision=0, limit=10, trade_offset=0):
        if type(trade_offset) is not int or not 0 <= trade_offset <= 4096:
            raise ValueError("trade checks require a bounded offset")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("review page supports 1–10 versions")
        records = await self.store.review_versions(
            group_id, account_ref=self.account_ref, after_revision=after_revision, limit=limit
        )
        versions = []
        for record in records:
            context = record.retrospective_context
            facts = record.facts
            versions.append(
                dict(
                    review=record.review.model_dump(mode="json"),
                    facts=dict(
                        cost_status=facts.cost_status.value,
                        reasons=facts.reasons,
                        inventory_quantity=str(facts.inventory_quantity),
                        unmatched_sold_quantity=str(facts.unmatched_sold_quantity),
                        realized_pnl_quote=str(facts.realized_pnl_quote)
                        if facts.realized_pnl_quote is not None
                        else None,
                        realized_pnl_usd=None,
                        quote_asset="USDT",
                        algorithm_version=facts.algorithm_version,
                    ),
                    trade_checks=[
                        check_view(check)
                        for check in record.trade_checks[trade_offset : trade_offset + 20]
                    ],
                    trade_count=len(record.trade_checks),
                    trade_offset=trade_offset,
                    has_more_checks=trade_offset + 20 < len(record.trade_checks),
                    next_trade_offset=min(trade_offset + 20, len(record.trade_checks)),
                    retrospective_context=dict(
                        captured_at=context.snapshot.captured_at.isoformat(),
                        observed_at=context.observed_at.isoformat(),
                        features=context.snapshot.features.model_dump(
                            mode="json", exclude={"snapshot_id"}
                        ),
                    )
                    if context
                    else None,
                )
            )
        return dict(
            mode=(await self.source.latest()).mode,
            group_id=group_id,
            versions=versions,
            next_revision=records[-1].review.revision if records else after_revision,
            may_have_more=len(records) == limit,
        )

    async def jobs(self):
        jobs = await self.store.review_jobs(self.account_ref, limit=50)
        return dict(
            mode=(await self.source.latest()).mode,
            jobs=[job_view(job) for job in jobs],
            record_limit=50,
            may_have_more=len(jobs) == 50,
        )
