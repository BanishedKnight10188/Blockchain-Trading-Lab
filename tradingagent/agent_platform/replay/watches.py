"""Deterministic replay using event reception time and the production evaluator."""

from collections.abc import Iterable
from datetime import timedelta

from agent_platform.domain.agent_events import WatchEvent, watch_event_id
from agent_platform.domain.models import DomainModel
from agent_platform.domain.watch_rules import evaluate_watch
from agent_platform.domain.watches import WatchDefinition, WatchFrame, WatchRecord, fact_hash


class WatchReplayReport(DomainModel):
    events: tuple[WatchEvent, ...]
    suppressed_event_ids: tuple[str, ...]
    final_states: tuple[tuple[str, str], ...]
    input_hashes: tuple[str, ...]
    rule_hashes: tuple[str, ...]
    conflicts: tuple[str, ...]

    @property
    def event_ids(self):
        return tuple(e.event_id for e in self.events)


def replay_watches(
    definitions: tuple[WatchDefinition, ...],
    frames: Iterable[WatchFrame],
    *,
    lane_id: str | None = None,
) -> WatchReplayReport:
    if len({d.watch_id for d in definitions}) != len(definitions):
        raise ValueError("replay watch identifiers must be unique")
    records = {d.watch_id: WatchRecord(definition=d) for d in definitions}
    events, suppressed, hashes = [], [], []
    facts, conflicts = {}, set()
    clock = None
    for frame in frames:
        now = frame.received_at
        if clock is not None and now < clock:
            raise ValueError("replay reception clock must be chronological")
        clock = now
        hashes.append(frame.content_hash)
        partition = (frame.market, frame.symbol, frame.interval, frame.price_kind)
        for candle in frame.candles:
            key = (partition, candle.opened_at)
            digest = fact_hash(candle)
            if key in facts and facts[key] != digest:
                conflicts.add(partition)
            facts.setdefault(key, digest)
        if frame.effective_quality == "data_conflict":
            conflicts.add(partition)
        effective = (
            frame.model_copy(update={"quality": "data_conflict"})
            if partition in conflicts
            else frame
        )
        for watch_id, watch in tuple(records.items()):
            d = watch.definition
            if lane_id is not None and d.lane_id != lane_id:
                continue
            if watch.state != "ARMED":
                continue
            if now >= d.expires_at:
                records[watch_id] = watch.model_copy(
                    update={"state": "EXPIRED", "revision": watch.revision + 1}
                )
                continue
            if (d.symbol, d.interval) != (frame.symbol, frame.interval):
                continue
            repair = (
                frame.candle_key == watch.last_candle_key
                and watch.reason in ("data_gap", "warming", "metric_unavailable")
                and effective.effective_quality == "ready"
            )
            if (
                watch.last_candle_key
                and frame.candle_key <= watch.last_candle_key
                and not repair
                and partition not in conflicts
            ):
                continue
            result = evaluate_watch(watch, effective, now)
            event_id = watch_event_id(d, frame) if result.emit_trigger else None
            records[watch_id] = watch.model_copy(
                update={
                    "state": result.next_state,
                    "revision": watch.revision + 1,
                    "last_candle_key": frame.candle_key,
                    "last_input_hash": result.input_hash,
                    "evaluated_at": now,
                    "reason": result.reason,
                    "event_id": event_id,
                }
            )
            if event_id is not None:
                event = WatchEvent(
                    event_id=event_id,
                    lane_id=d.lane_id,
                    session_id=d.session_id,
                    watch_id=watch_id,
                    definition_revision=d.version,
                    occurred_at=frame.occurred_at,
                    expires_at=frame.occurred_at + timedelta(seconds=120),
                    rule_hash=d.rule_hash,
                    frame=frame,
                    evaluation=result,
                )
                events.append(event)
                if now >= event.expires_at or not d.wake_agent:
                    suppressed.append(event_id)
    return WatchReplayReport(
        events=tuple(events),
        suppressed_event_ids=tuple(suppressed),
        final_states=tuple((key, record.state) for key, record in records.items()),
        input_hashes=tuple(hashes),
        rule_hashes=tuple(d.rule_hash for d in definitions),
        conflicts=tuple(":".join(p) for p in sorted(conflicts)),
    )
