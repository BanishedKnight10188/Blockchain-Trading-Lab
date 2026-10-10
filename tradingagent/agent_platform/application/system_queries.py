"""Safe diagnostic projection; unreadable budget is never presented as zero."""

from agent_platform.domain.market_archive import MarketRetentionPolicy
from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.domain.operating_modes import OperatingSettings
from agent_platform.domain.routing import RoutingPolicy
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.persistence import BudgetReadPort
from agent_platform.ports.sessions import PersistenceUnavailable


class SystemQueryService:
    def __init__(
        self,
        *,
        queries,
        budgets: BudgetReadPort,
        clock: ClockPort,
        review_jobs,
        policy: RoutingPolicy | None = None,
        runtime=None,
        archive=None,
        market_retention: MarketRetentionPolicy | None = None,
        model_modules: ModelModulesConfig | None = None,
        operation: OperatingSettings | None = None,
        controls=None,
        paper=None,
    ):
        self.queries, self.budgets, self.clock, self.review_jobs = (
            queries,
            budgets,
            clock,
            review_jobs,
        )
        self.policy = policy or RoutingPolicy()
        self.runtime, self.archive = runtime, archive
        self.market_retention = market_retention or MarketRetentionPolicy()
        self.model_modules = model_modules or ModelModulesConfig()
        self.operation = operation or OperatingSettings()
        self.controls = controls
        self.paper = paper

    async def current(self):
        budget_at = self.clock.utcnow()
        try:
            balance = await self.budgets.budget_balance(
                budget_at, daily_limit_usd=self.policy.daily_limit_usd
            )
            budget, status = balance.model_dump(mode="json"), "recorded"
        except (PersistenceUnavailable, ValueError):
            budget, status = None, "unavailable"
        overview = await self.queries.overview()
        paper = await self.paper.public_view() if self.paper is not None else None
        operation, modules = self.operation, self.model_modules
        if self.controls is not None:
            selected = await self.controls.current()
            operation = selected.operation
            modules = modules.model_copy(
                update={"jev": selected.jev, "jev_trader": selected.trader}
            )
        return dict(
            mode=overview.mode,
            generated_at=overview.generated_at.isoformat(),
            budget_as_of=budget_at.isoformat(),
            market=overview.market.model_dump(mode="json", exclude={"features"}),
            account=dict(
                status=overview.account.status,
                as_of=overview.account.as_of,
                attempted_at=overview.account.attempted_at,
                next_attempt_at=overview.account.next_attempt_at,
                history_complete=overview.account.history_complete,
                error=overview.account.error,
            ),
            decision_runtime=overview.decision_runtime,
            budget=budget,
            budget_status=status,
            paid_models_enabled=overview.paid_models_enabled
            or bool(paper and paper["paid_models_enabled"]),
            paper=paper,
            jev_status=overview.jev_status,
            routing=dict(
                policy_version=self.policy.version,
                daily_limit_usd=str(self.policy.daily_limit_usd),
                hourly_call_limit=self.policy.hourly_call_limit,
                configured_routes=len(self.policy.routes),
            ),
            model_modules=modules.public_state(),
            operation=operation.public_state(trader_enabled=modules.jev_trader.enabled),
            review_worker=dict(
                running=self.review_jobs.running, error=self.review_jobs.last_failure
            ),
            runtime=(await self.runtime.health()).model_dump(mode="json")
            if self.runtime is not None
            else None,
            archive=dict(
                enabled=self.archive is not None,
                running=self.archive.running if self.archive is not None else False,
                error=self.archive.last_failure if self.archive is not None else None,
                observations_written=self.archive.observations_written
                if self.archive is not None
                else 0,
                observations_removed=self.archive.observations_removed
                if self.archive is not None
                else 0,
                pending_count=self.archive.pending_count if self.archive is not None else 0,
                raw_retention_days=self.market_retention.raw_days,
                minute_retention_days=self.market_retention.minute_days,
            ),
        )
