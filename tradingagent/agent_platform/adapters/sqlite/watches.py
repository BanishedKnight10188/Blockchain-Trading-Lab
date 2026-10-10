"""Short atomic Watch transactions, immutable versions and conflict tombstones."""

from contextlib import closing
from datetime import timedelta

from agent_platform.domain.agent_events import EventDelivery, WatchEvent, watch_event_id
from agent_platform.domain.common import utc_datetime
from agent_platform.domain.watch_rules import evaluate_watch
from agent_platform.domain.watches import WatchDefinition, WatchFrame, WatchRecord, fact_hash
from agent_platform.ports.sessions import RevisionConflict

from .events import SqliteStore


def load_watch(db, watch_id):
    row = db.execute("SELECT body FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
    if row is None:
        raise ValueError("watch not found")
    return WatchRecord.model_validate_json(row["body"])


def save_watch(db, watch):
    db.execute(
        "UPDATE watches SET revision=?,state=?,body=? WHERE watch_id=?",
        (watch.revision, watch.state, watch.model_dump_json(), watch.definition.watch_id),
    )


def check_revision(watch, expected):
    if type(expected) is not int or expected != watch.revision:
        raise RevisionConflict("watch revision changed")


def suppress_pending(db, watch_id, reason):
    rows = db.execute(
        "SELECT d.body FROM agent_event_deliveries d JOIN watch_events e "
        "ON e.event_id=d.event_id WHERE e.watch_id=? AND d.status='QUEUED'",
        (watch_id,),
    ).fetchall()
    for row in rows:
        delivery = EventDelivery.model_validate_json(row["body"]).model_copy(
            update={"status": "SUPPRESSED", "reason": reason}
        )
        db.execute(
            "UPDATE agent_event_deliveries SET status=?,body=? WHERE event_id=?",
            (delivery.status, delivery.model_dump_json(), delivery.event_id),
        )


class SqliteWatchStore(SqliteStore):
    async def partition_healthy(self, watch_id):
        def read():
            with closing(self._connect()) as db:
                d = load_watch(db, watch_id).definition
                key = f"{d.market}:{d.symbol}:{d.interval}:{d.price_kind}"
                return (
                    db.execute(
                        "SELECT 1 FROM watch_partitions WHERE partition_key=?", (key,)
                    ).fetchone()
                    is None
                )

        return await self._io(read)

    async def list_all(self, lane_id, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("watch page must be bounded")

        def read():
            with closing(self._connect()) as db:
                return tuple(
                    WatchRecord.model_validate_json(r[0])
                    for r in db.execute(
                        "SELECT body FROM watches WHERE lane_id=? ORDER BY rowid DESC LIMIT ?",
                        (lane_id, limit),
                    )
                )

        return await self._io(read)

    @staticmethod
    def _capacity(db, lane_id):
        if (
            db.execute(
                "SELECT count(*) FROM watches WHERE lane_id=? AND state='ARMED'", (lane_id,)
            ).fetchone()[0]
            >= 20
        ):
            raise ValueError("watch lane capacity is 20")

    async def create(self, definition):
        definition = WatchDefinition.model_validate_json(definition.model_dump_json())
        if definition.version != 1:
            raise ValueError("new watch starts at definition revision 1")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM watches WHERE watch_id=?", (definition.watch_id,)
                ).fetchone():
                    raise ValueError("watch already exists")
                self._capacity(db, definition.lane_id)
                watch = WatchRecord(definition=definition)
                db.execute(
                    "INSERT INTO watches VALUES(?,?,?,?,?)",
                    (
                        definition.watch_id,
                        definition.lane_id,
                        1,
                        watch.state,
                        watch.model_dump_json(),
                    ),
                )
                db.execute(
                    "INSERT INTO watch_definitions VALUES(?,?,?)",
                    (definition.watch_id, 1, definition.model_dump_json()),
                )
                return watch

        return await self._io(write)

    async def get(self, watch_id):
        def read():
            with closing(self._connect()) as db:
                return load_watch(db, watch_id)

        return await self._io(read)

    async def list_active(self, lane_id):
        def read():
            with closing(self._connect()) as db:
                return tuple(
                    WatchRecord.model_validate_json(r["body"])
                    for r in db.execute(
                        "SELECT body FROM watches WHERE lane_id=? AND state='ARMED' "
                        "ORDER BY watch_id",
                        (lane_id,),
                    )
                )

        return await self._io(read)

    async def list_monitored(self, lane_id, now):
        now = utc_datetime(now)

        def read():
            with closing(self._connect()) as db:
                records = [
                    WatchRecord.model_validate_json(row[0])
                    for row in db.execute(
                        "SELECT body FROM watches WHERE lane_id=? AND state='ARMED' "
                        "ORDER BY watch_id",
                        (lane_id,),
                    )
                ]
                pending = db.execute(
                    "SELECT w.body AS watch_body,e.body AS event_body "
                    "FROM watches w JOIN watch_events e ON e.watch_id=w.watch_id "
                    "JOIN agent_event_deliveries d ON d.event_id=e.event_id "
                    "WHERE w.lane_id=? AND w.state='TRIGGERED' "
                    "AND d.status IN ('QUEUED','LEASED','DISPATCHED') ORDER BY w.watch_id",
                    (lane_id,),
                ).fetchall()
                for row in pending:
                    event = WatchEvent.model_validate_json(row["event_body"])
                    if now < event.expires_at:
                        records.append(WatchRecord.model_validate_json(row["watch_body"]))
                return tuple(records)

        return await self._io(read)

    async def replace(self, definition, expected_revision):
        definition = WatchDefinition.model_validate_json(definition.model_dump_json())

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                old = load_watch(db, definition.watch_id)
                check_revision(old, expected_revision)
                if old.state != "ARMED" or definition.version != old.definition.version + 1:
                    raise ValueError("only armed watches can receive the next definition version")
                if (definition.lane_id, definition.session_id) != (
                    old.definition.lane_id,
                    old.definition.session_id,
                ):
                    raise ValueError("watch ownership cannot change")
                watch = WatchRecord(definition=definition, revision=old.revision + 1)
                db.execute(
                    "INSERT INTO watch_definitions VALUES(?,?,?)",
                    (definition.watch_id, definition.version, definition.model_dump_json()),
                )
                save_watch(db, watch)
                return watch

        return await self._io(write)

    async def cancel(self, watch_id, expected_revision, at):
        at = utc_datetime(at)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                watch = load_watch(db, watch_id)
                check_revision(watch, expected_revision)
                if watch.state in ("CANCELLED", "INVALIDATED", "EXPIRED"):
                    return watch
                watch = watch.model_copy(
                    update={
                        "state": "CANCELLED",
                        "revision": watch.revision + 1,
                        "reason": "cancelled",
                        "evaluated_at": at,
                    }
                )
                save_watch(db, watch)
                suppress_pending(db, watch_id, "watch_cancelled")
                return watch

        return await self._io(write)

    async def expire(self, lane_id, now):
        now = utc_datetime(now)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                results = []
                for row in db.execute(
                    "SELECT body FROM watches WHERE lane_id=? AND state='ARMED'", (lane_id,)
                ).fetchall():
                    watch = WatchRecord.model_validate_json(row["body"])
                    if now >= watch.definition.expires_at:
                        watch = watch.model_copy(
                            update={
                                "state": "EXPIRED",
                                "revision": watch.revision + 1,
                                "reason": "expired",
                                "evaluated_at": now,
                            }
                        )
                        save_watch(db, watch)
                        results.append(watch)
                return tuple(results)

        return await self._io(write)

    @staticmethod
    def _record_inputs(db, frame):
        partition = f"{frame.market}:{frame.symbol}:{frame.interval}:{frame.price_kind}"
        conflict = frame.effective_quality == "data_conflict"
        for candle in frame.candles:
            key = candle.opened_at.isoformat()
            digest = fact_hash(candle)
            row = db.execute(
                "SELECT content_hash FROM watch_inputs WHERE partition_key=? AND candle_key=?",
                (partition, key),
            ).fetchone()
            if row is not None and row[0] != digest:
                conflict = True
            db.execute("INSERT OR IGNORE INTO watch_inputs VALUES(?,?,?)", (partition, key, digest))
        if conflict:
            db.execute("INSERT OR IGNORE INTO watch_partitions VALUES(?,1)", (partition,))
        return (
            db.execute(
                "SELECT 1 FROM watch_partitions WHERE partition_key=?", (partition,)
            ).fetchone()
            is not None
        )

    async def commit_evaluation(self, watch_id, expected_revision, frame, result):
        frame = WatchFrame.model_validate_json(frame.model_dump_json())
        if result.evaluated_at < frame.received_at:
            raise ValueError("evaluation precedes reception")

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                old = load_watch(db, watch_id)
                check_revision(old, expected_revision)
                if evaluate_watch(old, frame, result.evaluated_at) != result:
                    raise ValueError("evaluation does not match persisted watch and input")
                conflict = self._record_inputs(db, frame)
                if conflict:
                    suppress_pending(db, watch_id, "data_conflict")
                    if old.state != "ARMED":
                        return old
                    result_checked = evaluate_watch(
                        old,
                        frame.model_copy(update={"quality": "data_conflict"}),
                        result.evaluated_at,
                    )
                else:
                    result_checked = result
                if old.last_candle_key is not None and not conflict:
                    repair = (
                        frame.candle_key == old.last_candle_key
                        and old.reason in ("data_gap", "warming", "metric_unavailable")
                        and frame.effective_quality == "ready"
                    )
                    if (
                        frame.candle_key <= old.last_candle_key
                        and not repair
                        and result_checked.next_state == old.state
                    ):
                        return old
                if old.state != "ARMED":
                    return old
                event_id = (
                    watch_event_id(old.definition, frame) if result_checked.emit_trigger else None
                )
                watch = old.model_copy(
                    update={
                        "revision": old.revision + 1,
                        "state": result_checked.next_state,
                        "last_candle_key": frame.candle_key,
                        "last_input_hash": frame.content_hash,
                        "reason": result_checked.reason,
                        "evaluated_at": result_checked.evaluated_at,
                        "event_id": event_id,
                    }
                )
                save_watch(db, watch)
                if event_id is not None:
                    event = WatchEvent(
                        event_id=event_id,
                        lane_id=old.definition.lane_id,
                        session_id=old.definition.session_id,
                        watch_id=watch_id,
                        definition_revision=old.definition.version,
                        occurred_at=frame.occurred_at,
                        expires_at=frame.occurred_at + timedelta(seconds=120),
                        rule_hash=old.definition.rule_hash,
                        frame=frame,
                        evaluation=result_checked,
                    )
                    db.execute(
                        "INSERT INTO watch_events(event_id,watch_id,lane_id,body) VALUES(?,?,?,?)",
                        (event_id, watch_id, event.lane_id, event.model_dump_json()),
                    )
                    suppressed = (
                        result_checked.evaluated_at >= event.expires_at
                        or not old.definition.wake_agent
                    )
                    delivery = EventDelivery(
                        event_id=event_id,
                        status="SUPPRESSED" if suppressed else "QUEUED",
                        reason="stale_or_no_wake" if suppressed else None,
                    )
                    db.execute(
                        "INSERT INTO agent_event_deliveries VALUES(?,?,?,?)",
                        (event_id, event.lane_id, delivery.status, delivery.model_dump_json()),
                    )
                return watch

        return await self._io(write)
