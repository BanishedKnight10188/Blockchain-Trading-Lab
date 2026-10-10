"""CAS projections share a transaction with their audit and the existing session authority."""

import sqlite3
from contextlib import closing

from agent_platform.domain.common import required_identifier
from agent_platform.domain.events import JournalEvent, SessionJournalEvent, StateRecord, StateType
from agent_platform.domain.sessions import AgentSession
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import ActiveSessionExists, RevisionConflict

from .events import SqliteStore
from .sessions import SqliteSessionStore


class SqliteStateStore(SqliteStore):
    @staticmethod
    def _load(connection: sqlite3.Connection, key: str) -> StateRecord | None:
        session_row = connection.execute(
            "SELECT body FROM sessions WHERE session_id=?", (key,)
        ).fetchone()
        if session_row is not None:
            session = AgentSession.model_validate_json(session_row["body"])
            return StateRecord(
                key=session.session_id,
                revision=session.revision,
                state_type=StateType.SESSION,
                state=session,
                updated_at=session.updated_at,
            )
        row = connection.execute("SELECT body FROM domain_states WHERE key=?", (key,)).fetchone()
        return StateRecord.model_validate_json(row["body"]) if row else None

    async def load(self, key: str) -> StateRecord | None:
        checked_key = required_identifier(key)

        def read():
            with closing(self._connect()) as connection:
                return self._load(connection, checked_key)

        return await self._io(read)

    @staticmethod
    def _validate_update(previous: StateRecord, record: StateRecord) -> None:
        if previous.state_type != record.state_type or record.updated_at < previous.updated_at:
            raise ValueError("state type and chronology cannot be replaced")
        if record.state_type == StateType.RECOMMENDATION:
            expected = previous.state.transition(record.state.status, record.state.updated_at)
            if record.state != expected:
                raise ValueError("recommendation update cannot rewrite its original evidence")
        elif record.state_type == StateType.REVIEW:
            if record.state.parent_review_id != previous.state.review_id:
                raise ValueError("review update must reference the preceding version")
        elif record.state_type == StateType.REVIEW_JOB:
            if record.state.identity != previous.state.identity:
                raise ValueError("review job identity cannot change")
        elif record.state_type == StateType.ATTRIBUTION:
            if record.state.identity != previous.state.identity:
                raise ValueError("attribution scope cannot change")
        elif record.state_type == StateType.USER_REPORT:
            if record.state.report != previous.state.report:
                raise ValueError("verification cannot rewrite the original user report")
        elif record.state_type in (
            StateType.PAPER_TRIAL,
            StateType.FEEDBACK,
            StateType.ATTRIBUTION_CHANGE,
            StateType.REPORT_VERIFICATION,
            StateType.REVIEW_GROUP,
            StateType.REVIEW_RECORD,
        ):
            raise ValueError("user operation is an immutable fact")

    @staticmethod
    def _save_session(
        connection: sqlite3.Connection,
        record: StateRecord,
        previous: StateRecord | None,
        event: JournalEvent,
    ) -> None:
        session = record.state
        if previous is None:
            if session.status != "configured" or session.style_revision != 1:
                raise ValueError("initial session must be configured at its first style revision")
            occupied = connection.execute(
                "SELECT 1 FROM sessions WHERE status<>'closed'"
            ).fetchone()
            if occupied:
                raise ActiveSessionExists("an open session already exists")
            connection.execute(
                "INSERT INTO sessions(session_id, revision, status, body) VALUES(?,?,?,?)",
                (
                    session.session_id,
                    session.revision,
                    session.status.value,
                    session.model_dump_json(),
                ),
            )
            kind = "created"
        else:
            original = previous.state
            if session.style != original.style:
                kind = "style_changed"
            else:
                kind = "state_changed"
            SqliteSessionStore._validate_session_change(original, session, kind)
            connection.execute(
                "UPDATE sessions SET revision=?, status=?, body=? "
                "WHERE session_id=? AND revision=?",
                (
                    session.revision,
                    session.status.value,
                    session.model_dump_json(),
                    session.session_id,
                    previous.revision,
                ),
            )
        SqliteSessionStore._append(
            connection,
            SessionJournalEvent(
                event_id=event.event_id,
                kind=kind,
                session=session,
                occurred_at=record.updated_at,
            ),
        )

    async def save(
        self, record: StateRecord, expected_revision: int, event: JournalEvent
    ) -> StateRecord:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected revision must be a nonnegative integer")
        checked = StateRecord.model_validate_json(record.model_dump_json())
        audit = JournalEvent.model_validate_json(event.model_dump_json())
        if (
            checked.revision != expected_revision + 1
            or audit.kind != "state_changed"
            or audit.payload != checked
            or audit.occurred_at != checked.updated_at
        ):
            raise ValueError("state revision and audit must describe the same update")

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                prior_event = connection.execute(
                    "SELECT body FROM journal_events WHERE event_id=?", (audit.event_id,)
                ).fetchone()
                if prior_event is not None:
                    if JournalEvent.model_validate_json(prior_event["body"]) != audit:
                        raise EventIdentityConflict(
                            "event identifier already refers to another fact"
                        )
                    committed = connection.execute(
                        "SELECT 1 FROM state_commits WHERE event_id=?", (audit.event_id,)
                    ).fetchone()
                    if committed is not None:
                        return checked
                previous = self._load(connection, checked.key)
                if (previous.revision if previous else 0) != expected_revision:
                    raise RevisionConflict("state changed; reload before saving")
                if previous is not None:
                    self._validate_update(previous, checked)
                elif (
                    checked.state_type == StateType.RECOMMENDATION
                    and checked.state.status != "created"
                ):
                    raise ValueError("initial recommendation must start at created")
                self._append(connection, audit)
                if checked.state_type == StateType.SESSION:
                    self._save_session(connection, checked, previous, audit)
                else:
                    connection.execute(
                        "INSERT INTO domain_states(key, revision, state_type, body, event_id) "
                        "VALUES(?,?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET revision=excluded.revision, "
                        "state_type=excluded.state_type, body=excluded.body, "
                        "event_id=excluded.event_id",
                        (
                            checked.key,
                            checked.revision,
                            checked.state_type.value,
                            checked.model_dump_json(),
                            audit.event_id,
                        ),
                    )
                connection.execute(
                    "INSERT INTO state_commits(event_id) VALUES(?)", (audit.event_id,)
                )
                return checked

        return await self._io(write)
