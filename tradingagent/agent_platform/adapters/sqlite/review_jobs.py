"""Persistent explicitly chosen jobs share the atomic rule-review transaction."""

from contextlib import closing
from datetime import timedelta

from agent_platform.domain.common import live_account_ref, required_identifier, utc_datetime
from agent_platform.domain.events import JournalEvent, StateRecord
from agent_platform.domain.review_jobs import review_job_identity
from agent_platform.domain.review_records import ReviewProposal, rule_review_explanation
from agent_platform.domain.reviews import ReviewJob, ReviewJobStatus
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .reviews import SqliteReviewStore


class SqliteReviewJobStore(SqliteReviewStore):
    def _job(self, connection, identity, scope):
        state = self._load(connection, identity)
        if state is None:
            return None
        if state.state_type != "review_job":
            raise EventIdentityConflict("job identity refers to another state type")
        job = state.state
        group = self._group(connection, job.trade_group_id, scope)
        if group is None:
            return None
        if job.job_id != review_job_identity(job.trade_group_id, job.kind, job.scheduled_for):
            raise EventIdentityConflict("job has another scheduling identity")
        original = self._committed_state(connection, identity + ":scheduled")
        if (
            original.state_type != "review_job"
            or original.revision != 1
            or original.state.status != "pending"
        ):
            raise EventIdentityConflict("job has no original pending schedule")
        self._fact(
            connection,
            identity + ":scheduled:fact",
            "review_job_recorded",
            original.state,
            original.updated_at,
        )
        if (
            original.state.model_copy(
                update={
                    "status": job.status,
                    "review_id": job.review_id,
                    "failure_reason": job.failure_reason,
                }
            )
            != job
        ):
            raise EventIdentityConflict("job transition rewrote its original schedule")
        phase = ":scheduled" if job.status == "pending" else ":terminal"
        row = connection.execute(
            "SELECT event_id FROM domain_states WHERE key=?", (identity,)
        ).fetchone()
        if row["event_id"] != identity + phase or state != self._committed_state(
            connection, identity + phase
        ):
            raise EventIdentityConflict("job current state is not its committed transition")
        if phase == ":scheduled":
            if state != original:
                raise EventIdentityConflict("pending job changed its original revision")
        else:
            if state.revision != 2 or state.updated_at < original.updated_at:
                raise EventIdentityConflict("job terminal transition has an invalid revision")
            self._fact(
                connection,
                identity + ":terminal:fact",
                "review_job_recorded",
                job,
                state.updated_at,
            )
            if job.status == "done":
                record = self._record(connection, job.review_id, scope)
                if (
                    record is None
                    or record.review.trade_group_id != job.trade_group_id
                    or record.review.kind != job.kind
                    or record.review.data_cutoff != job.scheduled_for
                    or state.updated_at < job.scheduled_for
                    or state.updated_at < record.review.generated_at
                ):
                    raise EventIdentityConflict("completed job lacks its exact committed review")
            elif job.status not in ("failed", "canceled"):
                raise EventIdentityConflict("job transition has no terminal result")
        return state, original

    async def review_schedule(self, job_id, *, account_ref):
        identity, scope = required_identifier(job_id), live_account_ref(account_ref)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                found = self._job(connection, identity, scope)
                return found[1].state if found else None

        return await self._io(read)

    async def schedule_review_job(self, job, *, account_ref):
        checked, scope = (
            ReviewJob.model_validate_json(job.model_dump_json()),
            live_account_ref(account_ref),
        )

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                prior = self._job(connection, checked.job_id, scope)
                if prior:
                    original = prior[1].state
                    if checked.model_copy(update={"created_at": original.created_at}) != original:
                        raise RequestIdentityConflict("job already has another scheduling input")
                    return original
                if self._load(connection, checked.job_id) is not None:
                    raise RequestIdentityConflict("job belongs to another scope")
                group = self._group(connection, checked.trade_group_id, scope)
                if (
                    group is None
                    or checked.status != "pending"
                    or checked.failure_reason is not None
                    or checked.job_id
                    != review_job_identity(
                        checked.trade_group_id, checked.kind, checked.scheduled_for
                    )
                    or checked.scheduled_for < group.facts.data_cutoff
                    or not timedelta(0) <= self._now() - checked.created_at <= timedelta(seconds=60)
                ):
                    raise ValueError("job schedule requires valid local group evidence")
                self._empty_phases(
                    connection, (checked.job_id + ":scheduled", checked.job_id + ":scheduled:fact")
                )
                state = StateRecord(
                    key=checked.job_id,
                    revision=1,
                    state_type="review_job",
                    state=checked,
                    updated_at=checked.created_at,
                )
                self._feedback_state(connection, state, checked.job_id + ":scheduled")
                self._append(
                    connection,
                    JournalEvent(
                        event_id=checked.job_id + ":scheduled:fact",
                        aggregate_id=checked.job_id,
                        kind="review_job_recorded",
                        payload=checked,
                        occurred_at=checked.created_at,
                    ),
                )
                return checked

        return await self._io(write)

    def _terminal_job(self, connection, state, job):
        self._empty_phases(connection, (job.job_id + ":terminal", job.job_id + ":terminal:fact"))
        updated = StateRecord(
            key=job.job_id,
            revision=state.revision + 1,
            state_type="review_job",
            state=job,
            updated_at=self._now(),
        )
        if updated.updated_at < state.updated_at:
            raise ValueError("job completion clock moved backwards")
        if job.status == "done":
            record = self._record(
                connection, job.review_id, self._group_scope(connection, job.trade_group_id)
            )
            if (
                updated.updated_at < job.scheduled_for
                or updated.updated_at < record.review.generated_at
            ):
                raise ValueError("completion precedes the actual review result")
        self._feedback_state(connection, updated, job.job_id + ":terminal")
        self._append(
            connection,
            JournalEvent(
                event_id=job.job_id + ":terminal:fact",
                aggregate_id=job.job_id,
                kind="review_job_recorded",
                payload=job,
                occurred_at=updated.updated_at,
            ),
        )
        return job

    def _group_scope(self, connection, group_id):
        state = self._load(connection, self._group_key(group_id))
        if state is None or state.state_type != "review_group":
            raise EventIdentityConflict("job group is missing")
        return state.state.group.account_ref

    async def run_review_job(self, job_id, *, account_ref):
        identity, scope = required_identifier(job_id), live_account_ref(account_ref)

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                found = self._job(connection, identity, scope)
                if found is None:
                    raise ValueError("review job does not exist in this account")
                state = found[0]
                job = state.state
                if job.status != "pending":
                    return job
                if job.scheduled_for > self._now():
                    raise ValueError("review job is not due")
                group = self._group(connection, job.trade_group_id, scope)
                proposal = ReviewProposal(
                    trade_group_id=job.trade_group_id,
                    account_ref=scope,
                    kind=job.kind,
                    data_cutoff=job.scheduled_for,
                    generated_at=self._now(),
                    explanation=rule_review_explanation(group.facts.cost_status, job.kind),
                )
                connection.execute("SAVEPOINT review_job_generation")
                try:
                    record = self._record_review_tx(connection, proposal)
                except (EventIdentityConflict, RequestIdentityConflict):
                    raise
                except ValueError:
                    connection.execute("ROLLBACK TO review_job_generation")
                    connection.execute("RELEASE review_job_generation")
                    failed = job.model_copy(
                        update={
                            "status": ReviewJobStatus.FAILED,
                            "failure_reason": "review_conditions_unsatisfied",
                        }
                    )
                    return self._terminal_job(connection, state, failed)
                connection.execute("RELEASE review_job_generation")
                done = job.model_copy(
                    update={"status": ReviewJobStatus.DONE, "review_id": record.review.review_id}
                )
                return self._terminal_job(connection, state, done)

        return await self._io(write)

    async def cancel_review_job(self, job_id, expected_revision, *, account_ref):
        identity, scope = required_identifier(job_id), live_account_ref(account_ref)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("job cancellation requires an integer revision")

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                found = self._job(connection, identity, scope)
                if found is None:
                    raise ValueError("review job does not exist")
                state = found[0]
                if state.state.status == "canceled" and expected_revision == 1:
                    return state.state
                if state.revision != expected_revision:
                    raise RevisionConflict("review job changed before cancellation")
                if state.state.status != "pending":
                    raise ValueError("only pending jobs can be canceled")
                return self._terminal_job(
                    connection,
                    state,
                    state.state.model_copy(update={"status": ReviewJobStatus.CANCELED}),
                )

        return await self._io(write)

    async def review_jobs(self, account_ref, *, due_before=None, limit=50):
        scope = live_account_ref(account_ref)
        cutoff = utc_datetime(due_before) if due_before else None
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("job query limit must be 1–50")

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                query = (
                    "SELECT d.key FROM domain_states d WHERE d.state_type='review_job' AND EXISTS("
                    "SELECT 1 FROM domain_states g WHERE g.state_type='review_group' "
                    "AND json_extract(g.body,'$.state.group.account_ref')=? "
                    "AND json_extract(g.body,'$.state.group.trade_group_id')="
                    "json_extract(d.body,'$.state.trade_group_id'))"
                )
                arguments = [scope]
                if cutoff:
                    query += (
                        " AND json_extract(d.body,'$.state.status')='pending' AND "
                        + self._before_cutoff("json_extract(d.body,'$.state.scheduled_for')")
                    )
                    arguments.extend(self._cutoff_parameters(cutoff))
                stamp = "json_extract(d.body,'$.state.scheduled_for')"
                query += (
                    f" ORDER BY substr({stamp},1,19), CASE WHEN substr({stamp},20,1)='.' "
                    f"THEN CAST(substr(substr({stamp},21,6)||'000000',1,6) AS INTEGER) "
                    "ELSE 0 END,d.key LIMIT ?"
                )
                rows = connection.execute(query, (*arguments, limit)).fetchall()
                jobs = [self._job(connection, row["key"], scope)[0].state for row in rows]
                return tuple(sorted(jobs, key=lambda job: (job.scheduled_for, job.job_id)))

        return await self._io(read)
