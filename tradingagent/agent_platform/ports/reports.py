"""Local reported executions and verification never import exchange facts."""

from typing import Protocol

from agent_platform.domain.models import UtcDateTime
from agent_platform.domain.reports import ReportReceipt, UserReportedTrade


class ReportStorePort(Protocol):
    async def record_report(self, report: UserReportedTrade) -> ReportReceipt: ...

    async def report(self, report_id: str, *, account_ref: str) -> ReportReceipt | None: ...

    async def verify_report(
        self,
        report_id: str,
        operation_id: str,
        expected_revision: int,
        *,
        account_ref: str,
        checked_at: UtcDateTime,
    ) -> ReportReceipt: ...
