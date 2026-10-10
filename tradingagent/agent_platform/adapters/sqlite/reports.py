"""Exact-ID reconciliation appends evidence without creating or overwriting a fill."""

from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.account import ObservedTrade
from agent_platform.domain.common import live_account_ref, required_identifier, utc_datetime
from agent_platform.domain.events import JournalEvent, StateRecord
from agent_platform.domain.ledger_views import MAX_LEDGER_SEQUENCE, LedgerData, TradeLedgerEntry
from agent_platform.domain.reports import (
    ReportReceipt,
    ReportState,
    ReportVerification,
    UserReportedTrade,
)
from agent_platform.domain.reviews import TradeAttribution
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .attribution import SqliteAttributionStore


class SqliteReportStore(SqliteAttributionStore):
    async def ledger(self, account_ref, *, after_sequence=0, limit=50):
        scope = live_account_ref(account_ref)
        if (
            type(after_sequence) is not int
            or not 0 <= after_sequence <= MAX_LEDGER_SEQUENCE
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ValueError("local record cursor and limit must be bounded")

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT sequence,body FROM observed_trades "
                    "WHERE venue='binance' AND market_type='spot' AND account_ref=? "
                    "AND symbol='BTCUSDT' AND sequence>? ORDER BY sequence LIMIT ?",
                    (scope, after_sequence, limit + 1),
                ).fetchall()
                entries = []
                for row in rows[:limit]:
                    trade = ObservedTrade.model_validate_json(row["body"])
                    initial = TradeAttribution(
                        account_ref=scope, symbol=trade.symbol, trade_id=trade.trade_id
                    )
                    entries.append(
                        TradeLedgerEntry(
                            trade=trade, attribution=self._current_attribution(connection, initial)
                        )
                    )
                feedback_rows = connection.execute(
                    "SELECT body FROM domain_states WHERE state_type='feedback' "
                    "AND json_extract(body,'$.state.account_ref')=? ORDER BY rowid DESC LIMIT ?",
                    (scope, limit),
                ).fetchall()
                feedback = [
                    self._feedback(
                        connection,
                        StateRecord.model_validate_json(row["body"]).state.feedback.feedback_id,
                        scope,
                    )
                    for row in feedback_rows
                ]
                report_rows = connection.execute(
                    "SELECT body FROM domain_states WHERE state_type='user_report' "
                    "AND json_extract(body,'$.state.report.account_ref')=? "
                    "ORDER BY rowid DESC LIMIT ?",
                    (scope, limit),
                ).fetchall()
                reports = [
                    self._current_report(
                        connection,
                        self._report(
                            connection,
                            StateRecord.model_validate_json(row["body"]).state.report.report_id,
                            scope,
                        )[0],
                    )
                    for row in report_rows
                ]
                return LedgerData(
                    trades=tuple(entries),
                    feedback=tuple(feedback),
                    reports=tuple(reports),
                    next_trade_sequence=rows[min(limit, len(rows)) - 1]["sequence"]
                    if rows
                    else after_sequence,
                    has_more_trades=len(rows) > limit,
                )

        return await self._io(read)

    @staticmethod
    def report_identity(report_id, phase):
        return "user-report:" + sha256(report_id.encode()).hexdigest() + ":" + phase

    @staticmethod
    def verification_identity(operation_id, phase):
        return "report-verification:" + sha256(operation_id.encode()).hexdigest() + ":" + phase

    def _committed_state(self, connection, event_id):
        row = connection.execute(
            "SELECT j.body FROM journal_events j JOIN state_commits s "
            "ON j.event_id=s.event_id WHERE j.event_id=?",
            (event_id,),
        ).fetchone()
        return JournalEvent.model_validate_json(row["body"]).payload if row else None

    def _report(self, connection, report_id, account_ref):
        key = "user-report:" + sha256(report_id.encode()).hexdigest()
        current = self._load(connection, key)
        if current is None:
            return None
        if current.state_type != "user_report" or current.state.report.account_ref != account_ref:
            raise RequestIdentityConflict("user report belongs to another account")
        identity = self.report_identity(report_id, "state")
        initial = self._committed_state(connection, identity)
        fact = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=?",
            (self.report_identity(report_id, "fact"),),
        ).fetchone()
        if (
            initial is None
            or initial.state_type != "user_report"
            or initial.key != current.key
            or initial.revision != 1
            or initial.state != ReportState(report=current.state.report)
            or fact is None
            or JournalEvent.model_validate_json(fact["body"]).payload != current.state.report
        ):
            raise EventIdentityConflict("user report has no complete original audit")
        return current, initial

    def _current_report(self, connection, current):
        report = current.state.report
        if current.revision == 1:
            return ReportReceipt(
                state=current.state,
                revision=1,
                event_id=self.report_identity(report.report_id, "fact"),
            )
        stored = connection.execute(
            "SELECT event_id FROM domain_states WHERE key=?", (current.key,)
        ).fetchone()
        event_id = stored["event_id"]
        if not event_id.startswith("report-verification:") or not event_id.endswith(":report"):
            raise EventIdentityConflict("current report has no verification projection")
        row = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=? AND kind='user_trade_verified'",
            (event_id.removesuffix(":report") + ":fact",),
        ).fetchone()
        if row is None:
            raise EventIdentityConflict("current report has no committed verification")
        operation = JournalEvent.model_validate_json(row["body"]).payload
        if stored["event_id"] != self.verification_identity(operation.operation_id, "report"):
            raise EventIdentityConflict("current report is not its verification projection")
        receipt = self._verification(
            connection,
            report.report_id,
            operation.operation_id,
            current.revision - 1,
            report.account_ref,
        )
        if receipt is None or receipt.state != current.state:
            raise EventIdentityConflict("current report verification is inconsistent")
        return receipt

    async def report(self, report_id: str, *, account_ref: str):
        identity, scope = required_identifier(report_id), live_account_ref(account_ref)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                known = self._report(connection, identity, scope)
                if known is None:
                    return None
                current = known[0]
                return self._current_report(connection, current)

        return await self._io(read)

    async def record_report(self, report: UserReportedTrade):
        checked = UserReportedTrade.model_validate_json(report.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                known = self._report(connection, checked.report_id, checked.account_ref)
                if known is not None:
                    if known[0].state.report != checked:
                        raise RequestIdentityConflict(
                            "user report cannot replace its original inputs"
                        )
                    return ReportReceipt(
                        state=known[1].state,
                        revision=1,
                        event_id=self.report_identity(checked.report_id, "fact"),
                    )
                for phase in ("state", "fact"):
                    if connection.execute(
                        "SELECT 1 FROM journal_events WHERE event_id=?",
                        (self.report_identity(checked.report_id, phase),),
                    ).fetchone():
                        raise EventIdentityConflict("uncommitted user report audit already exists")
                if not timedelta(0) <= self._now() - checked.reported_at <= timedelta(seconds=60):
                    raise ValueError("new user report time is not current")
                state = ReportState(report=checked)
                record = StateRecord(
                    key=state.aggregate_id,
                    revision=1,
                    state_type="user_report",
                    state=state,
                    updated_at=state.updated_at,
                )
                self._feedback_state(
                    connection, record, self.report_identity(checked.report_id, "state")
                )
                fact_id = self.report_identity(checked.report_id, "fact")
                self._append(
                    connection,
                    JournalEvent(
                        event_id=fact_id,
                        aggregate_id=checked.report_id,
                        kind="user_trade_reported",
                        payload=checked,
                        occurred_at=checked.reported_at,
                    ),
                )
                return ReportReceipt(state=state, revision=1, event_id=fact_id)

        return await self._io(write)

    @staticmethod
    def _trade(connection, report):
        if report.exchange_trade_id is None:
            return None
        row = connection.execute(
            "SELECT body FROM observed_trades WHERE venue='binance' "
            "AND market_type='spot' AND account_ref=? AND symbol=? AND trade_id=?",
            (report.account_ref, report.symbol, report.exchange_trade_id),
        ).fetchone()
        return ObservedTrade.model_validate_json(row["body"]) if row else None

    @staticmethod
    def _verification_state(report, trade, checked_at):
        if trade is None:
            reason = (
                "exchange_id_missing"
                if report.exchange_trade_id is None
                else "exchange_trade_not_imported"
            )
            return ReportState(report=report, checked_at=checked_at, reasons=(reason,))
        if (trade.account_ref, trade.market_type, trade.symbol, trade.trade_id) != (
            report.account_ref,
            report.market_type,
            report.symbol,
            report.exchange_trade_id,
        ):
            raise ValueError("verification evidence belongs to another scope")
        reasons = tuple(
            reason
            for field, reason in (
                ("price", "price_mismatch"),
                ("quantity", "quantity_mismatch"),
                ("side", "side_mismatch"),
                ("executed_at", "execution_time_mismatch"),
            )
            if getattr(report, field) != getattr(trade, field)
        )
        return ReportState(
            report=report,
            status="conflict" if reasons else "verified",
            checked_at=checked_at,
            reasons=reasons,
            matched_trade_id=None if reasons else trade.trade_id,
        )

    def _verification(self, connection, report_id, operation_id, expected_revision, account_ref):
        key = "report-verification:" + sha256(operation_id.encode()).hexdigest()
        marker = self._load(connection, key)
        if marker is None:
            return None
        if marker.state_type != "report_verification":
            raise RequestIdentityConflict("verification identifier is already used")
        operation = marker.state
        if (
            operation.state.report.report_id != report_id
            or operation.state.report.account_ref != account_ref
            or operation.expected_revision != expected_revision
        ):
            raise RequestIdentityConflict("verification identifier has different inputs")
        for phase in ("state", "report"):
            state = self._committed_state(
                connection, self.verification_identity(operation_id, phase)
            )
            if (
                state is None
                or phase == "state"
                and state != marker
                or phase == "report"
                and (
                    state.state_type != "user_report"
                    or state.state != operation.state
                    or state.revision != expected_revision + 1
                )
            ):
                raise EventIdentityConflict("verification state audit is incomplete")
        fact_id = self.verification_identity(operation_id, "fact")
        fact = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=?", (fact_id,)
        ).fetchone()
        if fact is None or JournalEvent.model_validate_json(fact["body"]).payload != operation:
            raise EventIdentityConflict("verification fact audit is missing")
        known = self._report(connection, report_id, account_ref)
        if known is None or known[0].state.report != operation.state.report:
            raise EventIdentityConflict("verification original report is missing")
        evidence = operation.exchange_evidence
        if evidence is not None and self._trade(connection, operation.state.report) != evidence:
            raise EventIdentityConflict("verification exchange evidence is not imported")
        if (
            self._verification_state(operation.state.report, evidence, operation.updated_at)
            != operation.state
        ):
            raise EventIdentityConflict("verification result does not match its frozen evidence")
        return ReportReceipt(
            state=operation.state, revision=expected_revision + 1, event_id=fact_id
        )

    async def verify_report(
        self, report_id, operation_id, expected_revision, *, account_ref, checked_at
    ):
        identity, operation, scope, at = (
            required_identifier(report_id),
            required_identifier(operation_id),
            live_account_ref(account_ref),
            utc_datetime(checked_at),
        )
        if type(expected_revision) is not int or expected_revision < 1 or len(operation) > 128:
            raise ValueError("verification revision and identity must be bounded")

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                prior = self._verification(
                    connection, identity, operation, expected_revision, scope
                )
                if prior is not None:
                    return prior
                for phase in ("state", "report", "fact"):
                    if connection.execute(
                        "SELECT 1 FROM journal_events WHERE event_id=?",
                        (self.verification_identity(operation, phase),),
                    ).fetchone():
                        raise EventIdentityConflict("uncommitted verification audit already exists")
                known = self._report(connection, identity, scope)
                if known is None:
                    raise ValueError("user report does not exist")
                current = known[0]
                self._current_report(connection, current)
                if current.revision != expected_revision:
                    raise RevisionConflict("report changed; reload before verifying")
                if at < current.updated_at or not timedelta(0) <= self._now() - at <= timedelta(
                    seconds=60
                ):
                    raise ValueError("verification time is not current")
                evidence = self._trade(connection, current.state.report)
                state = self._verification_state(current.state.report, evidence, at)
                record = StateRecord(
                    key=current.key,
                    revision=expected_revision + 1,
                    state_type="user_report",
                    state=state,
                    updated_at=at,
                )
                self._feedback_state(
                    connection, record, self.verification_identity(operation, "report")
                )
                verification = ReportVerification(
                    operation_id=operation,
                    expected_revision=expected_revision,
                    state=state,
                    exchange_evidence=evidence,
                )
                marker = StateRecord(
                    key=verification.aggregate_id,
                    revision=1,
                    state_type="report_verification",
                    state=verification,
                    updated_at=at,
                )
                self._feedback_state(
                    connection, marker, self.verification_identity(operation, "state")
                )
                fact_id = self.verification_identity(operation, "fact")
                self._append(
                    connection,
                    JournalEvent(
                        event_id=fact_id,
                        aggregate_id=operation,
                        kind="user_trade_verified",
                        payload=verification,
                        occurred_at=at,
                    ),
                )
                return ReportReceipt(state=state, revision=record.revision, event_id=fact_id)

        return await self._io(write)
