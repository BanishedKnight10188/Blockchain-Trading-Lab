"""Record explicit user reports and check only exact local exchange identities."""

from agent_platform.domain.common import live_account_ref
from agent_platform.domain.reports import ReportReceipt, UserReportedTrade
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.reports import ReportStorePort


class ReportService:
    def __init__(self, *, store: ReportStorePort, clock: ClockPort, account_ref: str):
        self.store, self.clock, self.account_ref = store, clock, live_account_ref(account_ref)

    async def record(self, report: UserReportedTrade) -> ReportReceipt:
        checked = UserReportedTrade.model_validate_json(report.model_dump_json())
        if checked.account_ref != self.account_ref:
            raise ValueError("report belongs to another account")
        return await self.store.record_report(checked)

    async def verify(
        self, report_id: str, operation_id: str, expected_revision: int
    ) -> ReportReceipt:
        return await self.store.verify_report(
            report_id,
            operation_id,
            expected_revision,
            account_ref=self.account_ref,
            checked_at=self.clock.utcnow(),
        )
