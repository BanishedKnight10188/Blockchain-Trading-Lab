"""SQLite outbox leases with bounded pre-dispatch recovery."""

from contextlib import closing
from datetime import timedelta
from uuid import uuid4

from agent_platform.domain.agent_events import EventDelivery, EventLease, WatchEvent
from agent_platform.domain.common import required_identifier, utc_datetime

from .events import SqliteStore
from .watches import load_watch


def load_event(db, event_id):
    row = db.execute("SELECT body FROM watch_events WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("event not found")
    return WatchEvent.model_validate_json(row[0])


def load_delivery(db, event_id):
    row = db.execute(
        "SELECT body FROM agent_event_deliveries WHERE event_id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise ValueError("delivery not found")
    return EventDelivery.model_validate_json(row[0])


def save_delivery(db, value):
    db.execute(
        "UPDATE agent_event_deliveries SET status=?,body=? WHERE event_id=?",
        (value.status, value.model_dump_json(), value.event_id),
    )


def duration(seconds):
    if type(seconds) is not int or not 1 <= seconds <= 120:
        raise ValueError("lease must be between 1 and 120 seconds")
    return timedelta(seconds=seconds)


def owned(db, lease, now):
    delivery = load_delivery(db, lease.event.event_id)
    if (
        delivery.status not in ("LEASED", "DISPATCHED")
        or delivery.lease_token != lease.lease_token
        or delivery.lease_until is None
        or now >= delivery.lease_until
    ):
        raise ValueError("event lease is stale")
    return delivery


def eligible(db, event, now):
    watch = load_watch(db, event.watch_id)
    partition = (
        f"{event.frame.market}:{event.frame.symbol}:{event.frame.interval}:{event.frame.price_kind}"
    )
    if watch.state != "TRIGGERED" or watch.definition.version != event.definition_revision:
        return "watch_retired"
    if db.execute("SELECT 1 FROM watch_partitions WHERE partition_key=?", (partition,)).fetchone():
        return "data_conflict"
    if now >= event.expires_at:
        return "event_expired"
    return None


class SqliteAgentEventStore(SqliteStore):
    async def recent(self, lane_id, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("event page must be bounded")

        def read():
            with closing(self._connect()) as db:
                return tuple(
                    EventDelivery.model_validate_json(r[0])
                    for r in db.execute(
                        "SELECT d.body FROM agent_event_deliveries d JOIN watch_events e ON "
                        "e.event_id=d.event_id "
                        "WHERE d.lane_id=? ORDER BY e.sequence DESC LIMIT ?",
                        (lane_id, limit),
                    )
                )

        return await self._io(read)

    @staticmethod
    def _recover(db, now):
        changes = []
        rows = db.execute(
            "SELECT body FROM agent_event_deliveries WHERE status IN "
            "('QUEUED','LEASED','DISPATCHED')"
        ).fetchall()
        for row in rows:
            delivery = EventDelivery.model_validate_json(row[0])
            event = load_event(db, delivery.event_id)
            status, reason = delivery.status, delivery.reason
            if status == "DISPATCHED":
                if now >= delivery.lease_until:
                    status, reason = "RECONCILING", "dispatch_outcome_unknown"
            elif eligible(db, event, now) is not None:
                status, reason = "SUPPRESSED", eligible(db, event, now)
            elif status == "LEASED" and now >= delivery.lease_until:
                status = "FAILED" if delivery.attempts >= 3 else "QUEUED"
                reason = "lease_expired"
            if status != delivery.status:
                delivery = delivery.model_copy(
                    update={
                        "status": status,
                        "reason": reason,
                        **(
                            {"lease_token": None, "lease_until": None} if status == "QUEUED" else {}
                        ),
                    }
                )
                save_delivery(db, delivery)
                changes.append(delivery)
        return tuple(changes)

    async def get(self, event_id):
        def read():
            with closing(self._connect()) as db:
                return load_event(db, event_id)

        return await self._io(read)

    async def delivery(self, event_id):
        def read():
            with closing(self._connect()) as db:
                return load_delivery(db, event_id)

        return await self._io(read)

    async def recover(self, now):
        now = utc_datetime(now)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                return self._recover(db, now)

        return await self._io(write)

    async def claim(self, lane_id, now, lease_seconds=120):
        now, span = utc_datetime(now), duration(lease_seconds)
        lane_id = required_identifier(lane_id)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._recover(db, now)
                # A queued event must keep its full lifetime while another run
                # owns the lane. Check under the same transaction as leasing.
                if db.execute(
                    "SELECT 1 FROM agent_runs WHERE lane_id=? "
                    "AND status IN ('RUNNING','RECONCILING')",
                    (lane_id,),
                ).fetchone():
                    return None
                row = db.execute(
                    "SELECT d.body FROM agent_event_deliveries d JOIN watch_events e "
                    "ON d.event_id=e.event_id WHERE d.lane_id=? AND d.status='QUEUED' "
                    "ORDER BY e.sequence LIMIT 1",
                    (lane_id,),
                ).fetchone()
                if row is None:
                    return None
                delivery = EventDelivery.model_validate_json(row[0])
                event = load_event(db, delivery.event_id)
                token, until = uuid4().hex, now + span
                delivery = delivery.model_copy(
                    update={
                        "status": "LEASED",
                        "attempts": delivery.attempts + 1,
                        "lease_token": token,
                        "lease_until": until,
                    }
                )
                save_delivery(db, delivery)
                return EventLease(event=event, lease_token=token, lease_until=until)

        return await self._io(write)

    async def release(self, lease, now):
        """Return a provably undispatched lease without spending a busy retry."""
        now = utc_datetime(now)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                delivery = owned(db, lease, now)
                if delivery.status != "LEASED":
                    raise ValueError("dispatched work cannot be released")
                reason = eligible(db, load_event(db, delivery.event_id), now)
                delivery = delivery.model_copy(
                    update={
                        "status": "SUPPRESSED" if reason else "QUEUED",
                        "reason": reason,
                        "attempts": max(0, delivery.attempts - 1),
                        "lease_token": None,
                        "lease_until": None,
                    }
                )
                save_delivery(db, delivery)
                return delivery

        return await self._io(write)

    async def _change(self, lease, now, change):
        now = utc_datetime(now)

        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                delivery = owned(db, lease, now)
                delivery = change(delivery, now)
                save_delivery(db, delivery)
                return delivery

        return await self._io(write)

    async def renew(self, lease, now, lease_seconds=120):
        span = duration(lease_seconds)
        result = await self._change(
            lease, now, lambda d, at: d.model_copy(update={"lease_until": at + span})
        )
        return EventLease(
            event=lease.event, lease_token=lease.lease_token, lease_until=result.lease_until
        )

    async def complete(self, lease, run_id, now):
        run_id = required_identifier(run_id)

        def change(delivery, at):
            if delivery.run_id is not None and delivery.run_id != run_id:
                raise ValueError("completion has a different run")
            return delivery.model_copy(update={"status": "COMPLETED", "run_id": run_id})

        await self._change(lease, now, change)

    async def mark_dispatched(self, lease, run_id, now):
        run_id = required_identifier(run_id)

        def change(delivery, at):
            if delivery.status != "LEASED":
                raise ValueError("event already dispatched")
            # Check persisted scope health in the same write transaction as
            # dispatch ownership. A long renewal cannot extend event authority.
            with closing(self._connect()) as reader:
                if eligible(reader, load_event(reader, delivery.event_id), at) is not None:
                    raise ValueError("watch event no longer permits dispatch")
            return delivery.model_copy(update={"status": "DISPATCHED", "run_id": run_id})

        await self._change(lease, now, change)

    async def fail(self, lease, reason, retryable, now):
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        reason = required_identifier(reason)

        def change(delivery, at):
            status = (
                "RECONCILING"
                if delivery.status == "DISPATCHED"
                else "QUEUED"
                if retryable and delivery.attempts < 3
                else "FAILED"
            )
            return delivery.model_copy(
                update={
                    "status": status,
                    "reason": reason,
                    **({"lease_token": None, "lease_until": None} if status == "QUEUED" else {}),
                }
            )

        return await self._change(lease, now, change)
