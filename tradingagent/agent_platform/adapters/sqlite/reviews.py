"""Atomic frozen import groups, per-version facts and current review projection."""

from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.account import ObservedTrade, TradeBatch
from agent_platform.domain.common import live_account_ref, required_identifier, utc_datetime
from agent_platform.domain.decision_requests import DecisionReadRecord, DecisionRequest
from agent_platform.domain.events import JournalEvent, StateRecord
from agent_platform.domain.fifo import FifoAnalyzer
from agent_platform.domain.review_checks import trade_review_check
from agent_platform.domain.review_records import (
    FrozenReviewGroup,
    ReviewContextEvidence,
    ReviewGroupPage,
    ReviewProposal,
    ReviewRecord,
)
from agent_platform.domain.reviews import ReviewKind, ReviewRevision, TradeAttribution
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict

from .reports import SqliteReportStore


class SqliteReviewStore(SqliteReportStore):
    @staticmethod
    def _before_cutoff(column):
        return (
            f"(substr({column},1,19)<? OR (substr({column},1,19)=? AND "
            f"CASE WHEN substr({column},20,1)='.' THEN "
            f"CAST(substr(substr({column},21,6)||'000000',1,6) AS INTEGER) "
            "ELSE 0 END <=?))"
        )

    @staticmethod
    def _cutoff_parameters(cutoff):
        seconds = cutoff.isoformat()[:19]
        return seconds, seconds, cutoff.microsecond

    @staticmethod
    def _group_key(group_id):
        return "review-group:" + sha256(required_identifier(group_id).encode()).hexdigest()

    @staticmethod
    def _committed_state(connection, event_id):
        row = connection.execute(
            "SELECT j.body FROM state_commits s JOIN journal_events j "
            "ON j.event_id=s.event_id WHERE s.event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise EventIdentityConflict("review state lacks its complete commit")
        event = JournalEvent.model_validate_json(row["body"])
        if event.kind != "state_changed" or event.occurred_at != event.payload.updated_at:
            raise EventIdentityConflict("review state audit is inconsistent")
        return event.payload

    @staticmethod
    def _fact(connection, identity, kind, payload, at):
        row = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=?", (identity,)
        ).fetchone()
        if row is None:
            raise EventIdentityConflict("review fact is not committed")
        event = JournalEvent.model_validate_json(row["body"])
        if event.kind != kind or event.payload != payload or event.occurred_at != at:
            raise EventIdentityConflict("review fact conflicts with its state")

    @staticmethod
    def _empty_phases(connection, identities):
        for identity in identities:
            if connection.execute(
                "SELECT 1 FROM journal_events WHERE event_id=?", (identity,)
            ).fetchone():
                raise EventIdentityConflict("review operation contains an incomplete phase")

    @staticmethod
    def _history(connection, scope, cutoff):
        rows = connection.execute(
            "SELECT body FROM observed_trades WHERE venue='binance' AND market_type='spot' "
            "AND account_ref=? AND symbol='BTCUSDT' AND "
            + SqliteReviewStore._before_cutoff("json_extract(body,'$.executed_at')")
            + " ORDER BY sequence LIMIT 4097",
            (scope, *SqliteReviewStore._cutoff_parameters(cutoff)),
        ).fetchall()
        if len(rows) > 4096:
            raise ValueError("review history exceeds the bounded grouping window")
        trades = [ObservedTrade.model_validate_json(row["body"]) for row in rows]
        trades = [trade for trade in trades if trade.executed_at <= cutoff]
        trades.sort(
            key=lambda item: (
                item.executed_at,
                int(item.trade_id) if item.trade_id.isascii() and item.trade_id.isdecimal() else 0,
            )
        )
        return TradeBatch(account_ref=scope, symbol="BTCUSDT", trades=tuple(trades))

    async def review_history(self, account_ref, cutoff):
        scope, cutoff = live_account_ref(account_ref), utc_datetime(cutoff)

        def read():
            with closing(self._connect()) as connection:
                return self._history(connection, scope, cutoff)

        return await self._io(read)

    @staticmethod
    def _source_trades(connection, group):
        for trade in group.group.trades:
            row = connection.execute(
                "SELECT body FROM observed_trades WHERE venue=? AND market_type=? "
                "AND account_ref=? AND symbol=? AND trade_id=?",
                (
                    trade.venue,
                    trade.market_type.value,
                    trade.account_ref,
                    trade.symbol,
                    trade.trade_id,
                ),
            ).fetchone()
            if row is None or ObservedTrade.model_validate_json(row["body"]) != trade:
                raise EventIdentityConflict("review contains an absent or changed exchange fact")
            attribution = TradeAttribution(
                account_ref=trade.account_ref, symbol="BTCUSDT", trade_id=trade.trade_id
            )
            initial = connection.execute(
                "SELECT body FROM journal_events WHERE event_id=?",
                ("trade-unclassified:" + attribution.aggregate_id,),
            ).fetchone()
            event = JournalEvent.model_validate_json(initial["body"]) if initial else None
            if (
                event is None
                or event.kind != "attribution_recorded"
                or event.payload != attribution
                or event.occurred_at > group.facts.data_cutoff
            ):
                raise ValueError("group cutoff precedes receipt of the actual exchange facts")

    def _group(self, connection, group_id, scope):
        key = self._group_key(group_id)
        state = self._load(connection, key)
        if state is None:
            return None
        if state.state_type != "review_group" or state.revision != 1:
            raise EventIdentityConflict("review group is not an immutable frozen record")
        group = state.state
        if group.group.account_ref != scope:
            return None
        if state != self._committed_state(connection, key + ":state"):
            raise EventIdentityConflict("frozen group commit differs from its projection")
        self._fact(connection, key + ":fact", "review_group_frozen", group, group.registered_at)
        self._source_trades(connection, group)
        if FifoAnalyzer().analyze(group.batch, group.facts.data_cutoff) != group.facts:
            raise EventIdentityConflict("frozen FIFO facts differ from the observed sources")
        return group

    async def review_group(self, group_id, *, account_ref):
        identity, scope = required_identifier(group_id), live_account_ref(account_ref)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                return self._group(connection, identity, scope)

        return await self._io(read)

    async def review_groups(self, account_ref, *, after_sequence=0, limit=50):
        scope = live_account_ref(account_ref)
        if (
            type(after_sequence) is not int
            or not 0 <= after_sequence <= 2**63 - 1
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ValueError("group pagination must be bounded")

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT sequence,event_id,body FROM journal_events "
                    "WHERE kind='review_group_frozen' AND sequence>? "
                    "AND json_extract(body,'$.payload.group.account_ref')=? "
                    "ORDER BY sequence LIMIT ?",
                    (after_sequence, scope, limit + 1),
                ).fetchall()
                groups = []
                for row in rows[:limit]:
                    candidate = JournalEvent.model_validate_json(row["body"]).payload
                    group = self._group(connection, candidate.group.trade_group_id, scope)
                    if group != candidate or row["event_id"] != candidate.aggregate_id + ":fact":
                        raise EventIdentityConflict("group listing contains an uncommitted fact")
                    groups.append(group)
                return ReviewGroupPage(
                    groups=tuple(groups),
                    has_more=len(rows) > limit,
                    next_sequence=rows[min(limit, len(rows)) - 1]["sequence"]
                    if rows
                    else after_sequence,
                )

        return await self._io(read)

    async def freeze_review_group(self, group):
        checked = FrozenReviewGroup.model_validate_json(group.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                prior = self._group(
                    connection, checked.group.trade_group_id, checked.group.account_ref
                )
                if prior:
                    if prior.model_copy(update={"registered_at": checked.registered_at}) != checked:
                        raise RequestIdentityConflict(
                            "group already refers to another frozen input"
                        )
                    return prior
                if self._load(connection, checked.aggregate_id) is not None:
                    raise RequestIdentityConflict("group belongs to another scope")
                self._empty_phases(
                    connection, (checked.aggregate_id + ":state", checked.aggregate_id + ":fact")
                )
                now = self._now()
                if not timedelta(0) <= now - checked.registered_at <= timedelta(seconds=60):
                    raise ValueError("group registration time is invalid")
                if checked.batch != self._history(
                    connection, checked.group.account_ref, checked.facts.data_cutoff
                ):
                    raise ValueError("local history changed before group registration")
                if (
                    FifoAnalyzer().analyze(checked.batch, checked.facts.data_cutoff)
                    != checked.facts
                ):
                    raise ValueError("group facts must be computed from the actual history")
                self._source_trades(connection, checked)
                state = StateRecord(
                    key=checked.aggregate_id,
                    state_type="review_group",
                    state=checked,
                    revision=1,
                    updated_at=checked.registered_at,
                )
                self._feedback_state(connection, state, checked.aggregate_id + ":state")
                self._append(
                    connection,
                    JournalEvent(
                        event_id=checked.aggregate_id + ":fact",
                        aggregate_id=checked.aggregate_id,
                        kind="review_group_frozen",
                        payload=checked,
                        occurred_at=checked.registered_at,
                    ),
                )
                return checked

        return await self._io(write)

    def _attribution_proof(self, connection, record):
        for attribution, revision, identity in zip(
            record.attributions,
            record.attribution_revisions,
            record.attribution_event_ids,
            strict=True,
        ):
            if revision == 1:
                if attribution != TradeAttribution(
                    account_ref=record.account_ref, symbol="BTCUSDT", trade_id=attribution.trade_id
                ):
                    raise EventIdentityConflict("original attribution was not unclassified")
                row = connection.execute(
                    "SELECT body FROM journal_events WHERE event_id=?", (identity,)
                ).fetchone()
                event = JournalEvent.model_validate_json(row["body"]) if row else None
                if (
                    identity != "trade-unclassified:" + attribution.aggregate_id
                    or event is None
                    or event.kind != "attribution_recorded"
                    or event.payload != attribution
                    or event.occurred_at > record.review.data_cutoff
                ):
                    raise EventIdentityConflict("original imported attribution has no audit")
            else:
                state = self._committed_state(connection, identity)
                if (
                    state.state_type != "attribution"
                    or state.state != attribution
                    or state.revision != revision
                    or state.updated_at > record.review.data_cutoff
                ):
                    raise EventIdentityConflict(
                        "review attribution differs from its committed version"
                    )
                key = identity.rsplit(":", 1)[0]
                marker = self._load(connection, key)
                if marker is None or marker.state_type != "attribution_change":
                    raise EventIdentityConflict("classified attribution lacks its operation")
                change = self._change(connection, marker.state.operation_id, record.account_ref)
                receipt = change[1] if change else None
                if (
                    receipt is None
                    or receipt.revision != revision
                    or receipt.attribution != attribution
                ):
                    raise EventIdentityConflict("attribution operation is not complete")

    def _record_core(self, connection, key, scope, group=None):
        state = self._load(connection, key)
        if state is None:
            return None
        if state.state_type != "review_record" or state.revision != 1:
            raise EventIdentityConflict("review version is not immutable")
        record = state.state
        if record.account_ref != scope:
            return None
        review = record.review
        if state != self._committed_state(connection, key + ":record"):
            raise EventIdentityConflict("review version lacks its record commit")
        projection = self._committed_state(connection, key + ":current")
        if (
            projection.state_type != "review"
            or projection.state != review
            or projection.key != review.trade_group_id
        ):
            raise EventIdentityConflict("review version lacks its projection commit")
        self._fact(connection, key + ":fact", "review_recorded", review, review.generated_at)
        group = group or self._group(connection, review.trade_group_id, scope)
        if (
            group is None
            or record.facts != group.facts
            or review.evidence_ids != self._evidence_ids(group, record.retrospective_context)
        ):
            raise EventIdentityConflict("review evidence differs from its frozen group")
        if tuple(item.trade_id for item in record.attributions) != tuple(
            item.trade_id for item in group.group.trades
        ):
            raise EventIdentityConflict("review attribution does not cover the actual group")
        self._attribution_proof(connection, record)
        if record.trade_checks != self._review_checks(connection, group, record.attributions):
            raise EventIdentityConflict("review checks differ from original advice and execution")
        if record.retrospective_context is not None:
            self._context_proof(connection, record.retrospective_context)
        return record

    @staticmethod
    def _evidence_ids(group, context):
        return tuple("trade:" + item.trade_id for item in group.group.trades) + (
            ("snapshot:" + context.snapshot.snapshot_id,) if context is not None else ()
        )

    def _context_proof(self, connection, context):
        row = connection.execute(
            "SELECT d.body,d.result_body,j.body AS claimed,c.body AS completed "
            "FROM decision_requests d "
            "JOIN journal_events j ON j.event_id=d.claim_event_id "
            "JOIN journal_events c ON c.event_id=d.completion_event_id "
            "WHERE d.request_id=? AND d.completion_event_id=?",
            (context.request_id, context.completion_event_id),
        ).fetchone()
        if row is None:
            raise EventIdentityConflict("retrospective snapshot lacks its actual decision")
        request = DecisionRequest.model_validate_json(row["body"])
        claim, event = (
            JournalEvent.model_validate_json(row[name]) for name in ("claimed", "completed")
        )
        if (
            claim.kind != "decision_claimed"
            or claim.event_id != self._identity(request.request_id, "claim")
            or claim.payload != request
            or claim.occurred_at != request.requested_at
            or event.kind != "decision_completed"
            or event.event_id != self._identity(request.request_id, "complete")
            or event.aggregate_id != request.request_id
            or event.occurred_at != event.payload.completed_at
            or event.payload.result.status != "published"
            or event.payload.result.model_dump_json() != row["result_body"]
            or event.payload.publication_snapshot != context.snapshot
            or event.payload.completed_at != context.observed_at
        ):
            raise EventIdentityConflict("retrospective snapshot differs from committed evidence")
        self._validate_completion(request, event.payload)

    def _review_context_at(self, connection, group, cutoff):
        captured = "json_extract(c.body,'$.payload.publication_snapshot.captured_at')"
        rows = connection.execute(
            "SELECT d.request_id,d.completion_event_id,c.body FROM decision_requests d "
            "JOIN journal_events c ON c.event_id=d.completion_event_id "
            "WHERE json_extract(c.body,'$.payload.result.status')='published' "
            "AND json_extract(c.body,'$.payload.publication_snapshot.account.account_ref')=? "
            "AND json_extract(c.body,'$.payload.publication_snapshot.account.market_type')='spot' "
            "AND json_extract(c.body,'$.payload.publication_snapshot.market.symbol')='BTCUSDT' AND "
            + self._before_cutoff("c.occurred_at")
            + " AND NOT "
            + self._before_cutoff(captured)
            + f" ORDER BY substr({captured},1,19) DESC, CASE WHEN substr({captured},20,1)='.' "
            + f"THEN CAST(substr(substr({captured},21,6)||'000000',1,6) AS INTEGER) "
            + "ELSE 0 END DESC, "
            + "c.sequence DESC LIMIT 1",
            (
                group.group.account_ref,
                *self._cutoff_parameters(cutoff),
                *self._cutoff_parameters(group.facts.data_cutoff),
            ),
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        completed = JournalEvent.model_validate_json(row["body"]).payload
        context = ReviewContextEvidence(
            request_id=row["request_id"],
            completion_event_id=row["completion_event_id"],
            observed_at=completed.completed_at,
            snapshot=completed.publication_snapshot,
        )
        self._context_proof(connection, context)
        return context

    def _review_checks(self, connection, group, attributions):
        decisions, checks = {}, []
        for trade, attribution in zip(group.group.trades, attributions, strict=True):
            identity = attribution.recommendation_id
            if identity is not None and identity not in decisions:
                row = connection.execute(
                    "SELECT d.body,d.result_body,c.body AS completed FROM decision_requests d "
                    "JOIN journal_events c ON c.event_id=d.completion_event_id "
                    "WHERE json_extract(d.result_body,'$.recommendation.recommendation_id')=?",
                    (identity,),
                ).fetchone()
                if row is None:
                    raise EventIdentityConflict("linked review lacks its published decision")
                event = JournalEvent.model_validate_json(row["completed"])
                request = DecisionRequest.model_validate_json(row["body"])
                if (
                    event.kind != "decision_completed"
                    or event.aggregate_id != request.request_id
                    or event.event_id != self._identity(request.request_id, "complete")
                    or event.occurred_at != event.payload.completed_at
                    or event.payload.result.model_dump_json() != row["result_body"]
                ):
                    raise EventIdentityConflict("review advice completion is inconsistent")
                decisions[identity] = DecisionReadRecord(request=request, completion=event.payload)
            checks.append(trade_review_check(trade, attribution, decisions.get(identity)))
        return tuple(checks)

    def _record(self, connection, key, scope):
        record = self._record_core(connection, key, scope)
        if record is None:
            return None
        if record.review.revision > 4096:
            raise EventIdentityConflict("review chain exceeds the bounded version history")
        group = self._group(connection, record.review.trade_group_id, scope)
        current = record
        while current.review.revision > 1:
            prior = self._record_core(connection, current.review.parent_review_id, scope, group)
            if (
                prior is None
                or prior.review.trade_group_id != current.review.trade_group_id
                or prior.review.revision != current.review.revision - 1
                or prior.review.data_cutoff > current.review.data_cutoff
                or prior.review.generated_at > current.review.generated_at
            ):
                raise EventIdentityConflict("review parent chain or chronology is inconsistent")
            current = prior
        return record

    def _attribution_at(self, connection, attribution, cutoff):
        current = self._current_attribution(connection, attribution)
        if current.updated_at <= cutoff:
            row = connection.execute(
                "SELECT event_id FROM domain_states WHERE key=?", (current.key,)
            ).fetchone()
            return current, row["event_id"]
        rows = connection.execute(
            "SELECT j.event_id,j.body FROM state_commits s JOIN journal_events j "
            "ON j.event_id=s.event_id WHERE j.aggregate_id=? "
            "AND json_extract(j.body,'$.payload.state_type')='attribution' AND "
            + self._before_cutoff("json_extract(j.body,'$.payload.updated_at')")
            + " ORDER BY json_extract(j.body,'$.payload.revision') DESC LIMIT 1",
            (attribution.aggregate_id, *self._cutoff_parameters(cutoff)),
        ).fetchall()
        if rows:
            state = JournalEvent.model_validate_json(rows[0]["body"]).payload
            return state, rows[0]["event_id"]
        identity = "trade-unclassified:" + attribution.aggregate_id
        row = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=?", (identity,)
        ).fetchone()
        event = JournalEvent.model_validate_json(row["body"]) if row else None
        if event is None or event.occurred_at > cutoff or event.payload != attribution:
            raise ValueError("no attribution evidence existed before the review cutoff")
        return StateRecord(
            key=attribution.aggregate_id,
            revision=1,
            state_type="attribution",
            state=attribution,
            updated_at=event.occurred_at,
        ), identity

    def _current_review(self, connection, group_id, scope):
        state = self._load(connection, group_id)
        if state is None:
            return None
        if state.state_type != "review":
            raise EventIdentityConflict("review projection identity is occupied")
        record = self._record(connection, state.state.review_id, scope)
        if record is None or record.review != state.state:
            raise EventIdentityConflict("current review has no complete version")
        return record.review

    async def record_review(self, proposal):
        checked = ReviewProposal.model_validate_json(proposal.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._record_review_tx(connection, checked)

        return await self._io(write)

    def _record_review_tx(self, connection, checked):
        prior = self._record(connection, checked.aggregate_id, checked.account_ref)
        if prior:
            if prior.review.explanation != checked.explanation:
                raise RequestIdentityConflict("review identity already has another explanation")
            return prior
        if self._load(connection, checked.aggregate_id) is not None:
            raise RequestIdentityConflict("review belongs to another scope")
        self._empty_phases(
            connection,
            tuple(checked.aggregate_id + ":" + phase for phase in ("record", "current", "fact")),
        )
        group = self._group(connection, checked.trade_group_id, checked.account_ref)
        now = self._now()
        if (
            group is None
            or not group.facts.data_cutoff <= checked.data_cutoff <= checked.generated_at <= now
        ):
            raise ValueError("review cannot use absent or future evidence")
        if now - checked.generated_at > timedelta(seconds=60):
            raise ValueError("review generation time is stale")
        current = self._current_review(connection, checked.trade_group_id, checked.account_ref)
        if current and current.revision >= 4096:
            raise ValueError("review version limit reached; preserve the last readable version")
        if current and (
            checked.kind == ReviewKind.INITIAL
            or checked.data_cutoff < current.data_cutoff
            or checked.generated_at < current.generated_at
        ):
            raise ValueError("review cannot replace the initial or regress chronology")
        if current is None and checked.kind == ReviewKind.FOLLOWUP:
            raise ValueError("followup requires an existing review")
        attributions, revisions, event_ids = [], [], []
        for trade in group.group.trades:
            attribution = TradeAttribution(
                account_ref=checked.account_ref, symbol="BTCUSDT", trade_id=trade.trade_id
            )
            state, identity = self._attribution_at(connection, attribution, checked.data_cutoff)
            attributions.append(state.state)
            revisions.append(state.revision)
            event_ids.append(identity)
        context = (
            self._review_context_at(connection, group, checked.data_cutoff)
            if checked.kind != ReviewKind.INITIAL
            else None
        )
        review = ReviewRevision(
            review_id=checked.aggregate_id,
            trade_group_id=checked.trade_group_id,
            revision=current.revision + 1 if current else 1,
            parent_review_id=current.review_id if current else None,
            kind=checked.kind,
            data_cutoff=checked.data_cutoff,
            generated_at=checked.generated_at,
            evidence_ids=self._evidence_ids(group, context),
            explanation=checked.explanation,
            cost_status=group.facts.cost_status,
        )
        record = ReviewRecord(
            review=review,
            account_ref=checked.account_ref,
            facts=group.facts,
            attributions=tuple(attributions),
            attribution_revisions=tuple(revisions),
            attribution_event_ids=tuple(event_ids),
            trade_checks=self._review_checks(connection, group, attributions),
            retrospective_context=context,
        )
        self._attribution_proof(connection, record)
        marker = StateRecord(
            key=checked.aggregate_id,
            state_type="review_record",
            state=record,
            revision=1,
            updated_at=review.generated_at,
        )
        projection = StateRecord(
            key=review.trade_group_id,
            state_type="review",
            state=review,
            revision=review.revision,
            updated_at=review.generated_at,
        )
        self._feedback_state(connection, marker, checked.aggregate_id + ":record")
        self._feedback_state(connection, projection, checked.aggregate_id + ":current")
        self._append(
            connection,
            JournalEvent(
                event_id=checked.aggregate_id + ":fact",
                aggregate_id=review.trade_group_id,
                kind="review_recorded",
                payload=review,
                occurred_at=review.generated_at,
            ),
        )
        return record

    async def review_versions(self, group_id, *, account_ref, after_revision=0, limit=50):
        identity, scope = required_identifier(group_id), live_account_ref(account_ref)
        if (
            type(after_revision) is not int
            or not 0 <= after_revision <= 2**63 - 1
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise ValueError("review pagination must be bounded")

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                self._current_review(connection, identity, scope)
                rows = connection.execute(
                    "SELECT key FROM domain_states WHERE state_type='review_record' "
                    "AND json_extract(body,'$.state.account_ref')=? "
                    "AND json_extract(body,'$.state.review.trade_group_id')=? "
                    "AND json_extract(body,'$.state.review.revision')>? "
                    "ORDER BY json_extract(body,'$.state.review.revision') LIMIT ?",
                    (scope, identity, after_revision, limit),
                ).fetchall()
                return tuple(self._record(connection, row["key"], scope) for row in rows)

        return await self._io(read)
