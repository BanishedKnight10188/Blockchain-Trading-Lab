"""Classifications reference imported fills and retain every correction audit."""

from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.account import ObservedTrade
from agent_platform.domain.attribution import AttributionChange
from agent_platform.domain.common import live_account_ref, required_identifier
from agent_platform.domain.events import JournalEvent, StateRecord
from agent_platform.domain.reviews import AttributionReceipt
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.sessions import RevisionConflict

from .feedback import SqliteFeedbackStore


class SqliteAttributionStore(SqliteFeedbackStore):
    @staticmethod
    def attribution_identity(operation_id: str, phase: str) -> str:
        return "attribution-change:" + sha256(operation_id.encode()).hexdigest() + ":" + phase

    def _validate_source(self, connection, attribution, recorded_at):
        row = connection.execute(
            "SELECT body FROM observed_trades WHERE venue=? AND market_type=? "
            "AND account_ref=? AND symbol=? AND trade_id=?",
            attribution.identity,
        ).fetchone()
        if row is None:
            raise ValueError("attribution requires an imported exchange fill")
        trade = ObservedTrade.model_validate_json(row["body"])
        if trade.executed_at > recorded_at:
            raise ValueError("attribution cannot precede execution")
        if attribution.recommendation_id is None:
            return
        row = connection.execute(
            "SELECT d.body,c.body AS completed FROM decision_requests d "
            "JOIN journal_events c ON c.event_id=d.completion_event_id "
            "WHERE json_extract(d.result_body,'$.recommendation.recommendation_id')=?",
            (attribution.recommendation_id,),
        ).fetchone()
        decision = self._read_record(connection, row) if row else None
        if (
            decision is None
            or decision.completion is None
            or decision.completion.result.status != "published"
            or (
                decision.request.snapshot.account.account_ref,
                decision.request.snapshot.account.market_type,
                decision.request.snapshot.market.symbol,
            )
            != (attribution.account_ref, attribution.market_type, attribution.symbol)
        ):
            raise ValueError("attribution recommendation provenance is missing or cross-scope")
        if decision.completion.completed_at > trade.executed_at:
            raise ValueError("source recommendation was not published before execution")

    def _change(self, connection, operation_id, account_ref):
        key = "attribution-change:" + sha256(operation_id.encode()).hexdigest()
        marker = self._load(connection, key)
        if marker is None:
            return None
        if (
            marker.state_type != "attribution_change"
            or marker.state.attribution.account_ref != account_ref
        ):
            raise RequestIdentityConflict("attribution operation belongs to another scope")
        change = marker.state
        fact_id = self.attribution_identity(operation_id, "fact")
        fact = connection.execute(
            "SELECT body FROM journal_events WHERE event_id=?", (fact_id,)
        ).fetchone()
        for phase in ("state", "attribution"):
            identity = self.attribution_identity(operation_id, phase)
            row = connection.execute(
                "SELECT j.body FROM journal_events j JOIN state_commits s "
                "ON s.event_id=j.event_id WHERE j.event_id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise EventIdentityConflict("attribution operation has incomplete commit evidence")
            audit = JournalEvent.model_validate_json(row["body"]).payload
            if (
                phase == "state"
                and audit != marker
                or phase == "attribution"
                and (
                    audit.state_type != "attribution"
                    or audit.state != change.attribution
                    or audit.revision != change.expected_revision + 1
                    or audit.updated_at != change.recorded_at
                )
            ):
                raise EventIdentityConflict("attribution operation has inconsistent audit")
        if (
            fact is None
            or JournalEvent.model_validate_json(fact["body"]).payload != change.attribution
        ):
            raise EventIdentityConflict("attribution fact audit is missing")
        self._validate_source(connection, change.attribution, change.recorded_at)
        return change, AttributionReceipt(
            attribution=change.attribution,
            revision=change.expected_revision + 1,
            event_id=fact_id,
            recorded_at=change.recorded_at,
        )

    async def attribution_change(self, operation_id: str, *, account_ref: str):
        identity, scope = required_identifier(operation_id), live_account_ref(account_ref)

        def read():
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                known = self._change(connection, identity, scope)
                return known[1] if known else None

        return await self._io(read)

    async def record_attribution(self, change: AttributionChange) -> AttributionReceipt:
        checked = AttributionChange.model_validate_json(change.model_dump_json())

        def write():
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                attribution = checked.attribution
                known = self._change(connection, checked.operation_id, attribution.account_ref)
                if known is not None:
                    if known[0] != checked:
                        raise RequestIdentityConflict(
                            "attribution identifier has different immutable inputs"
                        )
                    return known[1]
                for phase in ("fact", "state", "attribution"):
                    if connection.execute(
                        "SELECT 1 FROM journal_events WHERE event_id=?",
                        (self.attribution_identity(checked.operation_id, phase),),
                    ).fetchone():
                        raise EventIdentityConflict("uncommitted attribution audit already exists")
                if not timedelta(0) <= self._now() - checked.recorded_at <= timedelta(seconds=60):
                    raise ValueError("attribution operation time is not current")
                self._validate_source(connection, attribution, checked.recorded_at)
                previous = self._load(connection, attribution.aggregate_id)
                if (
                    previous is None
                    or previous.state_type != "attribution"
                    or previous.state.identity != attribution.identity
                ):
                    raise ValueError("imported attribution projection is missing")
                if previous.revision != checked.expected_revision:
                    raise RevisionConflict("attribution changed; reload before correcting")
                if checked.recorded_at < previous.updated_at:
                    raise ValueError("attribution correction cannot predate previous state")
                record = StateRecord(
                    key=attribution.aggregate_id,
                    revision=previous.revision + 1,
                    state_type="attribution",
                    state=attribution,
                    updated_at=checked.recorded_at,
                )
                self._feedback_state(
                    connection,
                    record,
                    self.attribution_identity(checked.operation_id, "attribution"),
                )
                marker = StateRecord(
                    key=checked.aggregate_id,
                    revision=1,
                    state_type="attribution_change",
                    state=checked,
                    updated_at=checked.recorded_at,
                )
                self._feedback_state(
                    connection, marker, self.attribution_identity(checked.operation_id, "state")
                )
                fact_id = self.attribution_identity(checked.operation_id, "fact")
                self._append(
                    connection,
                    JournalEvent(
                        event_id=fact_id,
                        aggregate_id=attribution.aggregate_id,
                        kind="attribution_recorded",
                        payload=attribution,
                        occurred_at=checked.recorded_at,
                    ),
                )
                if not timedelta(0) <= self._now() - checked.recorded_at <= timedelta(seconds=60):
                    raise ValueError("attribution operation expired during commit")
                return AttributionReceipt(
                    attribution=attribution,
                    revision=record.revision,
                    event_id=fact_id,
                    recorded_at=checked.recorded_at,
                )

        return await self._io(write)

    def _current_attribution(self, connection, attribution):
        current = self._load(connection, attribution.aggregate_id)
        if (
            current is None
            or current.state_type != "attribution"
            or current.state.identity != attribution.identity
        ):
            raise EventIdentityConflict("trade attribution projection is missing")
        if current.revision == 1:
            if current.state != attribution:
                raise EventIdentityConflict("initial trade attribution is not unclassified")
            return current
        row = connection.execute(
            "SELECT event_id FROM domain_states WHERE key=?", (current.key,)
        ).fetchone()
        event_id = row["event_id"]
        if not event_id.startswith("attribution-change:") or not event_id.endswith(":attribution"):
            raise EventIdentityConflict("trade attribution has no correction projection")
        marker = self._load(connection, event_id.removesuffix(":attribution"))
        if marker is None or marker.state_type != "attribution_change":
            raise EventIdentityConflict("trade attribution operation is missing")
        known = self._change(connection, marker.state.operation_id, attribution.account_ref)
        if (
            known is None
            or known[1].attribution != current.state
            or known[1].revision != current.revision
        ):
            raise EventIdentityConflict("trade attribution correction is inconsistent")
        return current
