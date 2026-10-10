"""Durable request ownership and publication share the authoritative write lock."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from agent_platform.domain.common import required_identifier, utc_datetime
from agent_platform.domain.costs import BudgetReservation, ModelUsage
from agent_platform.domain.decision_requests import (
    DecisionClaimReceipt,
    DecisionCompletion,
    DecisionReadRecord,
    DecisionRequest,
)
from agent_platform.domain.decisions import DecisionResult, Recommendation
from agent_platform.domain.events import JournalEvent, StateRecord
from agent_platform.ports.clock import ClockPort
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .observations import SqliteObservationStore


class SqliteDecisionStore(SqliteObservationStore):
    def __init__(self, path, *, clock: ClockPort | None = None):
        super().__init__(path)
        self._decision_clock = clock

    def _now(self):
        return (
            utc_datetime(self._decision_clock.utcnow())
            if self._decision_clock
            else datetime.now(UTC)
        )

    @staticmethod
    def _identity(request_id: str, phase: str) -> str:
        return "decision:" + sha256(request_id.encode()).hexdigest() + ":" + phase

    @staticmethod
    def _receipt(row, *, claimed=False):
        return DecisionClaimReceipt(
            request=DecisionRequest.model_validate_json(row["body"]),
            claimed=claimed,
            result=DecisionResult.model_validate_json(row["result_body"])
            if row["result_body"]
            else None,
        )

    async def decision(self, request_id: str) -> DecisionClaimReceipt | None:
        identity = required_identifier(request_id)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT * FROM decision_requests WHERE request_id=?",
                    (identity,),
                ).fetchone()
                return self._receipt(row) if row else None

        return await self._io(read)

    async def latest_decision(self, session_id: str) -> DecisionReadRecord | None:
        identity = required_identifier(session_id)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT d.body, c.body AS completed FROM decision_requests d "
                    "JOIN journal_events j ON j.event_id=d.claim_event_id "
                    "LEFT JOIN journal_events c ON c.event_id=d.completion_event_id "
                    "WHERE json_extract(d.body,'$.snapshot.session_id')=? "
                    "ORDER BY j.sequence DESC LIMIT 1",
                    (identity,),
                ).fetchone()
                if row is None:
                    return None
                return self._read_record(connection, row)

        return await self._io(read)

    def _read_record(self, connection, row):
        completion = (
            JournalEvent.model_validate_json(row["completed"]).payload if row["completed"] else None
        )
        advice = completion.result.recommendation if completion else None
        state = self._load(connection, advice.recommendation_id) if advice else None
        return DecisionReadRecord(
            request=DecisionRequest.model_validate_json(row["body"]),
            completion=completion,
            current_recommendation=state.state
            if state and state.state_type == "recommendation"
            else None,
        )

    async def claim(self, request: DecisionRequest) -> DecisionClaimReceipt:
        checked = DecisionRequest.model_validate_json(request.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM decision_requests WHERE request_id=?",
                    (checked.request_id,),
                ).fetchone()
                if row is not None:
                    receipt = self._receipt(row)
                    if receipt.request != checked:
                        raise RequestIdentityConflict(
                            "decision identifier has different immutable inputs"
                        )
                    return receipt
                identity = self._identity(checked.request_id, "claim")
                snapshot = checked.snapshot
                self._append(
                    connection,
                    JournalEvent(
                        event_id="decision-snapshot:"
                        + sha256(snapshot.snapshot_id.encode()).hexdigest(),
                        aggregate_id=snapshot.snapshot_id,
                        kind="decision_snapshot_recorded",
                        payload=snapshot,
                        occurred_at=snapshot.captured_at,
                    ),
                )
                self._append(
                    connection,
                    JournalEvent(
                        event_id=identity,
                        aggregate_id=checked.request_id,
                        kind="decision_claimed",
                        payload=checked,
                        occurred_at=checked.requested_at,
                    ),
                )
                connection.execute(
                    "INSERT INTO decision_requests(request_id,body,claim_event_id) VALUES(?,?,?)",
                    (checked.request_id, checked.model_dump_json(), identity),
                )
                return DecisionClaimReceipt(request=checked, claimed=True)

        return await self._io(write)

    @staticmethod
    def _validate_completion(request: DecisionRequest, completion: DecisionCompletion):
        result, original = completion.result, request.snapshot
        if result.request_id != request.request_id or result.snapshot_id != original.snapshot_id:
            raise ValueError("completion does not match claimed input")
        if completion.completed_at < request.requested_at:
            raise ValueError("completion predates claimed request")
        publication, advice = completion.publication_snapshot, result.recommendation
        if publication is not None and (
            publication.session_id != original.session_id
            or publication.account.account_ref != original.account.account_ref
            or publication.account.market_type != original.account.market_type
            or publication.market.symbol != original.market.symbol
            or publication.limits != original.limits
            or publication.trigger != original.trigger
        ):
            raise ValueError("publication changed immutable decision scope")
        if advice is not None and (
            advice.session_id != original.session_id
            or advice.style_revision != original.style_revision
            or advice.account_revision != original.account.account_revision
            or advice.created_at < request.requested_at
            or advice.expires_at
            > min(
                original.trigger.expires_at,
                original.captured_at + timedelta(seconds=advice.assessment.valid_for_seconds),
            )
            or not set(advice.assessment.evidence_ids) <= set(original.evidence_ids)
        ):
            raise ValueError("recommendation changed original authority or evidence")

    def _admit(self, connection, request, completion):
        result = completion.result
        if result.status != "published":
            return result
        now = self._now()
        if now < completion.completed_at or now >= completion.valid_until(request):
            return self._unavailable(result, "publication_expired")
        original, publication = request.snapshot, completion.publication_snapshot
        session = self._load(connection, original.session_id)
        changed = []
        if (
            session is None
            or session.state_type != "session"
            or (
                session.state.status != "running"
                or session.state.revision != original.session_revision
                or session.state.style_revision != original.style_revision
                or session.state.style != original.style
                or publication.session_revision != original.session_revision
                or publication.style_revision != original.style_revision
                or publication.style != original.style
            )
        ):
            changed.append("session_changed")
        account = self._account(
            connection, original.account.account_ref, original.account.market_type
        )
        if (
            account is None
            or account.status != "fresh"
            or account.account_revision == 0
            or not (timedelta(0) <= now - account.as_of <= timedelta(seconds=60))
        ):
            return self._unavailable(result, "account_unavailable")
        if (
            account.account_revision != original.account.account_revision
            or publication.account.account_revision != original.account.account_revision
            or self._balances(account) != self._balances(publication.account)
        ):
            changed.append("account_changed")
        if changed:
            return DecisionResult.model_validate(
                {
                    **result.model_dump(),
                    "status": "superseded",
                    "reasons": tuple(changed),
                    "recommendation": result.recommendation.transition(
                        "superseded", completion.completed_at
                    ),
                }
            )
        return result

    @staticmethod
    def _validate_usage(connection, request, completion):
        usage = completion.result.usage
        if usage is None:
            return
        route = request.route
        if (
            route.kind == "rule"
            or usage.route_id != route.route_id
            or usage.model_version != route.model_version
            or not request.requested_at <= usage.recorded_at <= completion.completed_at
            or usage.input_tokens > 131072
            or usage.output_tokens > 8192
        ):
            raise ValueError("usage does not match claimed model request")
        for amount in (usage.actual_cost_usd, usage.estimated_cost_usd):
            if amount is not None and (
                not amount.is_finite()
                or len(amount.as_tuple().digits) > 128
                or not -128 <= amount.as_tuple().exponent <= 128
            ):
                raise ValueError("usage amount is outside the bounded contract")
        row = connection.execute(
            "SELECT body,usage_body FROM budget_requests WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("usage requires its persisted budget reservation")
        reservation = BudgetReservation.model_validate_json(row["body"])
        budget = reservation.request
        if (
            budget.route_id != route.route_id
            or budget.price_version != route.price_version
            or budget.purpose != route.purpose
            or budget.estimated_cost_usd != usage.estimated_cost_usd
            or row["usage_body"] is None
            or ModelUsage.model_validate_json(row["usage_body"]) != usage
        ):
            raise ValueError("usage has not been settled against the claimed reservation")

    @staticmethod
    def _unavailable(result, reason):
        return DecisionResult.model_validate(
            {
                **result.model_dump(),
                "status": "unavailable",
                "recommendation": None,
                "reasons": (reason,),
            }
        )

    def _save_advice(self, connection, request_id, advice):
        if self._load(connection, advice.recommendation_id) is not None:
            raise RevisionConflict("recommendation identity already exists")
        initial = Recommendation.model_validate(
            {
                **advice.model_dump(),
                "status": "created",
                "updated_at": advice.created_at,
            }
        )
        final = initial.transition(advice.status, advice.updated_at)
        for revision, candidate, phase in ((1, initial, "created"), (2, final, "final")):
            record = StateRecord(
                key=candidate.recommendation_id,
                revision=revision,
                state_type="recommendation",
                state=candidate,
                updated_at=candidate.updated_at,
            )
            identity = self._identity(request_id, phase)
            self._append(
                connection,
                JournalEvent(
                    event_id=identity,
                    aggregate_id=record.key,
                    kind="state_changed",
                    payload=record,
                    occurred_at=record.updated_at,
                ),
            )
            connection.execute(
                "INSERT INTO domain_states(key,revision,state_type,body,event_id) "
                "VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "revision=excluded.revision,body=excluded.body,event_id=excluded.event_id",
                (record.key, revision, record.state_type.value, record.model_dump_json(), identity),
            )
            connection.execute("INSERT INTO state_commits(event_id) VALUES(?)", (identity,))

    async def finish(self, request_id: str, completion: DecisionCompletion) -> DecisionResult:
        identity = required_identifier(request_id)
        checked = DecisionCompletion.model_validate_json(completion.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM decision_requests WHERE request_id=?",
                    (identity,),
                ).fetchone()
                if row is None:
                    raise LookupError("decision request has not been claimed")
                if row["completion_body"] is not None:
                    if DecisionCompletion.model_validate_json(row["completion_body"]) != checked:
                        raise RequestIdentityConflict(
                            "decision completion cannot replace its original result"
                        )
                    return DecisionResult.model_validate_json(row["result_body"])
                request = DecisionRequest.model_validate_json(row["body"])
                for phase in ("created", "final", "complete", "publication-snapshot"):
                    if (
                        connection.execute(
                            "SELECT 1 FROM journal_events WHERE event_id=?",
                            (self._identity(identity, phase),),
                        ).fetchone()
                        is not None
                    ):
                        raise EventIdentityConflict("uncommitted publication audit already exists")
                self._validate_completion(request, checked)
                self._validate_usage(connection, request, checked)
                result = self._admit(connection, request, checked)
                connection.execute("SAVEPOINT publication")
                self._record_completion(connection, identity, checked, result)
                if result.status == "published":
                    final_check = self._admit(connection, request, checked)
                    if final_check.status != "published":
                        connection.execute("ROLLBACK TO publication")
                        result = final_check
                        self._record_completion(connection, identity, checked, result)
                connection.execute("RELEASE publication")
                return result

        return await self._io(write)

    def _record_completion(self, connection, identity, checked, result):
        if checked.publication_snapshot is not None:
            snapshot = checked.publication_snapshot
            self._append(
                connection,
                JournalEvent(
                    event_id="decision-snapshot:"
                    + sha256(snapshot.snapshot_id.encode()).hexdigest(),
                    aggregate_id=snapshot.snapshot_id,
                    kind="decision_snapshot_recorded",
                    payload=snapshot,
                    occurred_at=snapshot.captured_at,
                ),
            )
        if result.recommendation is not None:
            self._save_advice(connection, identity, result.recommendation)
        audit_completion = DecisionCompletion.model_validate(
            {
                **checked.model_dump(),
                "result": result,
            }
        )
        event_id = self._identity(identity, "complete")
        self._append(
            connection,
            JournalEvent(
                event_id=event_id,
                aggregate_id=identity,
                kind="decision_completed",
                payload=audit_completion,
                occurred_at=checked.completed_at,
            ),
        )
        connection.execute(
            "UPDATE decision_requests SET completion_body=?,result_body=?,"
            "completion_event_id=? WHERE request_id=?",
            (checked.model_dump_json(), result.model_dump_json(), event_id, identity),
        )
