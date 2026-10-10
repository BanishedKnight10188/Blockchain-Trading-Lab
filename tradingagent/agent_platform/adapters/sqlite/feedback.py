"""Feedback facts and the current recommendation projection commit together."""

from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.common import required_identifier
from agent_platform.domain.decisions import FeedbackReceipt
from agent_platform.domain.events import JournalEvent, StateRecord
from agent_platform.domain.feedback import FeedbackEvidence, FeedbackRecord
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .decisions import SqliteDecisionStore


class SqliteFeedbackStore(SqliteDecisionStore):
    @staticmethod
    def feedback_identity(feedback_id: str, phase: str) -> str:
        return "feedback:" + sha256(feedback_id.encode()).hexdigest() + ":" + phase

    def _feedback(self, connection, feedback_id, account_ref):
        key = "feedback:" + sha256(feedback_id.encode()).hexdigest()
        state = self._load(connection, key)
        if state is None:
            return None
        if state.state_type != "feedback" or state.state.account_ref != account_ref:
            raise RequestIdentityConflict("feedback identifier belongs to another scope")
        fact_id, state_id = (
            self.feedback_identity(feedback_id, phase) for phase in ("fact", "state")
        )
        fact = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=?", (fact_id,)
        ).fetchone()
        marker = connection.execute(
            "SELECT j.body FROM state_commits s JOIN journal_events j "
            "ON j.event_id=s.event_id WHERE s.event_id=?",
            (state_id,),
        ).fetchone()
        if (
            fact is None
            or marker is None
            or JournalEvent.model_validate_json(marker["body"]).payload != state
            or JournalEvent.model_validate_json(fact["body"]).payload != state.state.feedback
        ):
            raise EventIdentityConflict("feedback has no complete atomic audit")
        if state.state.feedback.recommendation_id is not None:
            advice_id = self.feedback_identity(feedback_id, "advice")
            row = connection.execute(
                "SELECT j.body FROM state_commits s JOIN journal_events j "
                "ON j.event_id=s.event_id WHERE s.event_id=?",
                (advice_id,),
            ).fetchone()
            if row is None:
                raise EventIdentityConflict("feedback recommendation commit is missing")
            advice = JournalEvent.model_validate_json(row["body"]).payload
            if (
                advice.state_type != "recommendation"
                or advice.key != state.state.feedback.recommendation_id
                or advice.state != state.state.recommendation_state
                or advice.revision != state.state.recommendation_revision
            ):
                raise EventIdentityConflict("feedback recommendation commit is inconsistent")
            row = connection.execute(
                "SELECT d.body,c.body AS completed FROM decision_requests d "
                "JOIN journal_events c ON c.event_id=d.completion_event_id "
                "WHERE json_extract(d.result_body,'$.recommendation.recommendation_id')=?",
                (advice.key,),
            ).fetchone()
            source = self._read_record(connection, row) if row else None
            original = (
                source.completion.result.recommendation if source and source.completion else None
            )
            if (
                original is None
                or source.request.snapshot.account.account_ref != account_ref
                or advice.state.model_dump(exclude={"status", "updated_at"})
                != original.model_dump(exclude={"status", "updated_at"})
            ):
                raise EventIdentityConflict("feedback recommendation provenance is missing")
        return FeedbackReceipt(feedback=state.state.feedback, event_id=fact_id)

    async def feedback(self, feedback_id: str, *, account_ref: str) -> FeedbackReceipt | None:
        identity, scope = required_identifier(feedback_id), required_identifier(account_ref)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                return self._feedback(connection, identity, scope)

        return await self._io(read)

    async def recommendation_decision(self, recommendation_id: str):
        identity = required_identifier(recommendation_id)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT d.body,c.body AS completed FROM decision_requests d "
                    "JOIN journal_events c ON c.event_id=d.completion_event_id "
                    "WHERE json_extract(d.result_body,'$.recommendation.recommendation_id')=?",
                    (identity,),
                ).fetchone()
                return self._read_record(connection, row) if row else None

        return await self._io(read)

    def _admit_feedback(self, connection, record, evidence):
        now = self._now()
        if not timedelta(0) <= now - record.feedback.recorded_at <= timedelta(seconds=60):
            raise ValueError("feedback time is not current")
        if record.feedback.recommendation_id is None:
            if evidence is not None:
                raise ValueError("independent feedback has no recommendation evidence")
            return None
        if evidence is None:
            raise ValueError("related feedback requires current evidence")
        request, completion, current = evidence.request, evidence.completion, evidence.current
        original, advice = request.snapshot, completion.result.recommendation
        row = connection.execute(
            "SELECT d.body,c.body AS completed FROM decision_requests d "
            "JOIN journal_events c ON c.event_id=d.completion_event_id WHERE d.request_id=?",
            (request.request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("feedback decision provenance is missing")
        stored = self._read_record(connection, row)
        if (
            stored.request != request
            or stored.completion != completion
            or completion.result.status != "published"
            or advice is None
            or advice.recommendation_id != record.feedback.recommendation_id
        ):
            raise ValueError("feedback changed its decision provenance")
        state = self._load(connection, advice.recommendation_id)
        if (
            state is None
            or state.state_type != "recommendation"
            or state.state.status != "published"
        ):
            raise ValueError("recommendation is no longer current")
        if state.revision != evidence.expected_revision:
            raise RevisionConflict("recommendation changed before feedback")
        if (
            state.state != advice
            or record.account_ref != original.account.account_ref
            or original.account.market_type != "spot"
            or original.market.symbol != "BTCUSDT"
        ):
            raise ValueError("feedback changed original authority or scope")
        if record.feedback.kind != "accepted":
            return state
        if current is None or evidence.risk is None:
            raise ValueError("acceptance requires current risk evidence")
        if (
            now < current.captured_at
            or now >= evidence.valid_until()
            or evidence.risk.snapshot_id != current.snapshot_id
            or not current.captured_at <= evidence.risk.evaluated_at <= now
            or not evidence.risk.permits_advice
        ):
            raise ValueError("feedback evidence is expired or disallowed")
        session = self._load(connection, original.session_id)
        if (
            session is None
            or session.state_type != "session"
            or session.state.status != "running"
            or session.revision != original.session_revision
            or session.state.style != original.style
            or session.state.style_revision != original.style_revision
        ):
            raise ValueError("feedback session authority changed")
        if (
            current.session_id,
            current.session_revision,
            current.style_revision,
            current.style,
            current.trigger,
            current.limits,
            current.market.symbol,
        ) != (
            original.session_id,
            original.session_revision,
            original.style_revision,
            original.style,
            original.trigger,
            original.limits,
            original.market.symbol,
        ):
            raise ValueError("feedback snapshot authority changed")
        account = self._account(connection, record.account_ref, original.account.market_type)
        if (
            account is None
            or account.status != "fresh"
            or not account.account_revision
            or not timedelta(0) <= now - account.as_of <= timedelta(seconds=60)
        ):
            raise ValueError("feedback account evidence unavailable")
        if (
            current.account.account_ref != record.account_ref
            or current.account.market_type != "spot"
            or account.account_revision != original.account.account_revision
            or current.account.account_revision != account.account_revision
            or self._balances(account) != self._balances(current.account)
            or self._balances(account) != self._balances(original.account)
        ):
            raise ValueError("feedback account authority changed")
        return state

    def _feedback_state(self, connection, state, event_id):
        self._append(
            connection,
            JournalEvent(
                event_id=event_id,
                aggregate_id=state.key,
                kind="state_changed",
                payload=state,
                occurred_at=state.updated_at,
            ),
        )
        connection.execute(
            "INSERT INTO domain_states(key,revision,state_type,body,event_id) VALUES(?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,"
            "body=excluded.body,event_id=excluded.event_id",
            (state.key, state.revision, state.state_type.value, state.model_dump_json(), event_id),
        )
        connection.execute("INSERT INTO state_commits(event_id) VALUES(?)", (event_id,))

    async def record_feedback(
        self, record: FeedbackRecord, evidence: FeedbackEvidence | None
    ) -> FeedbackReceipt:
        checked = FeedbackRecord.model_validate_json(record.model_dump_json())
        proof = (
            FeedbackEvidence.model_validate_json(evidence.model_dump_json()) if evidence else None
        )

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                feedback = checked.feedback
                prior = self._feedback(connection, feedback.feedback_id, checked.account_ref)
                if prior is not None:
                    if prior.feedback != feedback:
                        raise RequestIdentityConflict(
                            "feedback identifier has different immutable inputs"
                        )
                    return prior
                for phase in ("fact", "state", "advice"):
                    if connection.execute(
                        "SELECT 1 FROM journal_events WHERE event_id=?",
                        (self.feedback_identity(feedback.feedback_id, phase),),
                    ).fetchone():
                        raise EventIdentityConflict("uncommitted feedback audit already exists")
                state = self._admit_feedback(connection, checked, proof)
                committed = checked
                if state is not None:
                    status = "accepted" if feedback.kind == "accepted" else "rejected"
                    if (
                        feedback.kind != "accepted"
                        and feedback.recorded_at >= state.state.expires_at
                    ):
                        status = "expired"
                    changed = state.state.transition(status, feedback.recorded_at)
                    updated = StateRecord(
                        key=state.key,
                        revision=state.revision + 1,
                        state_type="recommendation",
                        state=changed,
                        updated_at=changed.updated_at,
                    )
                    self._feedback_state(
                        connection, updated, self.feedback_identity(feedback.feedback_id, "advice")
                    )
                    committed = checked.model_copy(
                        update={
                            "recommendation_state": changed,
                            "recommendation_revision": updated.revision,
                        }
                    )
                marker = StateRecord(
                    key=checked.aggregate_id,
                    revision=1,
                    state_type="feedback",
                    state=committed,
                    updated_at=feedback.recorded_at,
                )
                self._feedback_state(
                    connection, marker, self.feedback_identity(feedback.feedback_id, "state")
                )
                fact_id = self.feedback_identity(feedback.feedback_id, "fact")
                self._append(
                    connection,
                    JournalEvent(
                        event_id=fact_id,
                        aggregate_id=feedback.feedback_id,
                        kind="feedback_recorded",
                        payload=feedback,
                        occurred_at=feedback.recorded_at,
                    ),
                )
                now = self._now()
                if (
                    not timedelta(0) <= now - feedback.recorded_at <= timedelta(seconds=60)
                    or feedback.kind == "accepted"
                    and proof is not None
                    and (now < proof.current.captured_at or now >= proof.valid_until())
                ):
                    raise ValueError("feedback evidence expired during commit")
                return FeedbackReceipt(feedback=feedback, event_id=fact_id)

        return await self._io(write)
