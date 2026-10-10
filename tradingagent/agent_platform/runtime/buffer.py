"""Bounded event-time aggregation, independent quote clocks and frozen publications."""

import heapq
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from agent_platform.domain.common import exact_add, required_identifier, utc_datetime
from agent_platform.domain.market import (
    AggregateWindow,
    BookTicker,
    BufferUpdate,
    Candle,
    MarketEvent,
    MarketSnapshot,
    TradeTick,
)


@dataclass(frozen=True)
class BufferStats:
    seen_events: int
    pending_events: int
    pending_windows: int
    gap_count: int


@dataclass
class _Bar:
    opened_at: datetime
    interval: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    count: int
    complete: bool

    def add(self, tick: TradeTick, notional: Decimal) -> None:
        self.high, self.low, self.close = (
            max(self.high, tick.price),
            min(self.low, tick.price),
            tick.price,
        )
        self.volume = exact_add(self.volume, tick.quantity)
        self.quote_volume = exact_add(self.quote_volume, notional)
        self.count += 1

    def freeze(self, symbol: str) -> AggregateWindow:
        return AggregateWindow(
            interval_seconds=self.interval,
            trade_count=self.count,
            complete=self.complete,
            candle=Candle(
                symbol=symbol,
                opened_at=self.opened_at,
                closed_at=self.opened_at + timedelta(seconds=self.interval),
                open=self.open,
                high=self.high,
                low=self.low,
                close=self.close,
                volume=self.volume,
                quote_volume=self.quote_volume,
            ),
        )


def _bound(value: int, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError("buffer bounds must be nonnegative/positive integers")
    return value


def _same_fact(first: MarketEvent, second: MarketEvent) -> bool:
    return (
        first.event_id == second.event_id
        and first.symbol == second.symbol
        and first.source == second.source
        and first.payload == second.payload
        and first.time_quality == second.time_quality
        and first.stream_sequence == second.stream_sequence
        and (first.time_quality == "received" or first.occurred_at == second.occurred_at)
    )


class MarketBuffer:
    def __init__(
        self,
        symbol: str = "BTCUSDT",
        *,
        lateness_seconds: int = 2,
        seen_capacity: int = 4096,
        seen_ttl_seconds: int = 300,
        pending_capacity: int = 4096,
    ):
        self.symbol = required_identifier(symbol)
        self.lateness = timedelta(seconds=_bound(lateness_seconds, zero=True))
        self.seen_capacity = _bound(seen_capacity)
        self.seen_ttl = timedelta(seconds=_bound(seen_ttl_seconds))
        self.pending_capacity = _bound(pending_capacity)
        self._seen: OrderedDict[str, tuple[datetime, MarketEvent]] = OrderedDict()
        self._pending: list[tuple[datetime, int, str, MarketEvent, Decimal]] = []
        self._pending_by_id: dict[str, MarketEvent] = {}
        self._working: dict[int, dict[datetime, _Bar]] = {interval: {} for interval in (1, 5, 60)}
        self._bars = {
            interval: deque(maxlen=count) for interval, count in ((1, 300), (5, 60), (60, 120))
        }
        self._candles: dict[datetime, Candle] = {}
        self._candle_events: dict[datetime, MarketEvent] = {}
        self._trade: MarketEvent | None = None
        self._book: MarketEvent | None = None
        self._max_event_at: datetime | None = None
        self._observed_at: datetime | None = None
        self._coverage_start: datetime | None = None
        self._published: MarketSnapshot | None = None
        self._gap = False
        self._gap_since: datetime | None = None
        self._gap_count = 0

    @property
    def stats(self) -> BufferStats:
        return BufferStats(
            len(self._seen),
            len(self._pending),
            sum(map(len, self._working.values())),
            self._gap_count,
        )

    @property
    def watermark(self) -> datetime | None:
        return self._max_event_at - self.lateness if self._max_event_at else None

    def mark_gap(self, reason: str, at: datetime, *, missing_from: datetime | None = None) -> None:
        required_identifier(reason)
        timestamp = utc_datetime(at)
        start = timestamp if missing_from is None else utc_datetime(missing_from)
        if start > timestamp:
            raise ValueError("gap start is ahead of observation time")
        self._observe_arrival(timestamp)
        self._gap, self._gap_count = True, self._gap_count + 1
        self._gap_since = min(self._gap_since or start, start)
        for windows in self._working.values():
            for bar in windows.values():
                bar.complete = False

    def _observe_arrival(self, received_at: datetime) -> None:
        now = max(self._observed_at or received_at, received_at)
        self._observed_at = now
        cutoff = now - self.seen_ttl
        while self._seen and next(iter(self._seen.values()))[0] < cutoff:
            self._seen.popitem(last=False)

    def _remember(self, event: MarketEvent) -> None:
        self._observe_arrival(event.received_at)
        self._seen[event.event_id] = self._observed_at, event
        self._seen.move_to_end(event.event_id)
        while len(self._seen) > self.seen_capacity:
            self._seen.popitem(last=False)

    def _original(self, event_id: str) -> MarketEvent | None:
        cached = self._seen.get(event_id)
        if cached is not None:
            return cached[1]
        pending = self._pending_by_id.get(event_id)
        if pending is not None:
            return pending
        return next(
            (
                item
                for item in (self._trade, self._book, *self._candle_events.values())
                if item is not None and item.event_id == event_id
            ),
            None,
        )

    def _flush(self) -> None:
        watermark = self.watermark
        while self._pending and self._pending[0][0] < watermark:
            _, _, _, event, notional = heapq.heappop(self._pending)
            self._pending_by_id.pop(event.event_id)
            tick = event.payload
            for interval, windows in self._working.items():
                timestamp = event.occurred_at.replace(microsecond=0)
                opened_at = timestamp - timedelta(seconds=timestamp.second % interval)
                bar = windows.get(opened_at)
                if bar:
                    bar.add(tick, notional)
                else:
                    windows[opened_at] = _Bar(
                        opened_at,
                        interval,
                        tick.price,
                        tick.price,
                        tick.price,
                        tick.price,
                        tick.quantity,
                        notional,
                        1,
                        opened_at >= self._coverage_start and not self._gap,
                    )
        for interval, windows in self._working.items():
            for opened_at in sorted(tuple(windows)):
                if opened_at + timedelta(seconds=interval) <= watermark:
                    self._bars[interval].append(windows.pop(opened_at).freeze(self.symbol))

    def append(self, raw: MarketEvent) -> BufferUpdate:
        event = MarketEvent.model_validate_json(raw.model_dump_json())
        if event.symbol != self.symbol or event.occurred_at > event.received_at:
            raise ValueError("buffer event scope/time is incompatible")
        payload = event.payload
        if (
            isinstance(payload, Candle)
            and payload.is_closed
            and payload.closed_at - timedelta(milliseconds=1) > event.received_at
        ):
            raise ValueError("closed candle is ahead of reception time")
        if isinstance(payload, Candle) and (
            payload.closed_at - payload.opened_at != timedelta(minutes=1)
            or payload.opened_at.second != 0
            or payload.opened_at.microsecond != 0
        ):
            raise ValueError("authoritative candle must be an aligned minute")
        original = self._original(event.event_id)
        if original is not None:
            if not _same_fact(original, event):
                raise ValueError("event identity already refers to another market fact")
            self._observe_arrival(event.received_at)
            return BufferUpdate(event_id=event.event_id, duplicate=True)
        if isinstance(payload, Candle) and payload.is_closed:
            old = self._candles.get(payload.opened_at)
            if old is not None:
                if old != payload:
                    raise ValueError("closed minute cannot be overwritten")
                self._observe_arrival(event.received_at)
                return BufferUpdate(event_id=event.event_id, duplicate=True)
        if isinstance(payload, BookTicker) and self._book is not None:
            old = self._book
            if (
                old.source == event.source
                and old.stream_sequence is not None
                and event.stream_sequence is not None
                and event.stream_sequence <= old.stream_sequence
            ):
                if event.stream_sequence == old.stream_sequence:
                    if event.payload != old.payload:
                        raise ValueError("book sequence already refers to different quotes")
                    self._observe_arrival(event.received_at)
                    return BufferUpdate(event_id=event.event_id, duplicate=True)
                self.mark_gap(
                    "book_sequence_regression", event.received_at, missing_from=old.occurred_at
                )
                return BufferUpdate(
                    event_id=event.event_id, late=True, reason="book_sequence_regression"
                )
        if self.watermark is not None and event.occurred_at < self.watermark:
            self.mark_gap(
                "late_market_event",
                event.received_at,
                missing_from=payload.opened_at
                if isinstance(payload, Candle)
                else event.occurred_at,
            )
            return BufferUpdate(event_id=event.event_id, late=True, reason="late_market_event")
        notional = None
        if isinstance(payload, TradeTick):
            with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
                notional = payload.price * payload.quantity
            if len(self._pending) >= self.pending_capacity and self._pending[0][0] < (
                max(self._max_event_at, event.occurred_at) - self.lateness
            ):
                self._max_event_at = max(self._max_event_at, event.occurred_at)
                self._flush()
            if len(self._pending) >= self.pending_capacity:
                self.mark_gap(
                    "pending_capacity_exceeded", event.received_at, missing_from=event.occurred_at
                )
                return BufferUpdate(event_id=event.event_id, reason="pending_capacity_exceeded")
        self._remember(event)
        self._coverage_start = self._coverage_start or event.received_at
        self._max_event_at = max(self._max_event_at or event.occurred_at, event.occurred_at)
        if isinstance(payload, TradeTick):
            if self._trade is None or (
                event.occurred_at,
                event.stream_sequence or 0,
                event.event_id,
            ) > (self._trade.occurred_at, self._trade.stream_sequence or 0, self._trade.event_id):
                self._trade = event
            heapq.heappush(
                self._pending,
                (event.occurred_at, event.stream_sequence or 0, event.event_id, event, notional),
            )
            self._pending_by_id[event.event_id] = event
        elif isinstance(payload, BookTicker):
            if self._book is None or event.occurred_at >= self._book.occurred_at:
                self._book = event
        elif payload.is_closed:
            self._candles[payload.opened_at] = payload
            self._candles = dict(sorted(self._candles.items())[-120:])
            self._candle_events[payload.opened_at] = event
            self._candle_events = {key: self._candle_events[key] for key in self._candles}
        self._flush()
        return BufferUpdate(event_id=event.event_id, accepted=True)

    def bars(self, interval_seconds: int) -> tuple[AggregateWindow, ...]:
        if type(interval_seconds) is not int or interval_seconds not in self._bars:
            raise ValueError("unsupported aggregate interval")
        return tuple(self._bars[interval_seconds])

    def reconcile(self, events: Iterable[MarketEvent], as_of: datetime) -> tuple[BufferUpdate, ...]:
        timestamp = utc_datetime(as_of)
        if self._observed_at is not None and timestamp < self._observed_at:
            raise ValueError("reconciliation time predates delivered data")
        candidates, combined, results = [], dict(self._candles), []
        identities: dict[str, MarketEvent] = {}
        provenance = dict(self._candle_events)
        for index, raw in enumerate(events):
            if index >= 120:
                raise ValueError("reconciliation batch exceeds retained minute capacity")
            event = MarketEvent.model_validate_json(raw.model_dump_json())
            candle = event.payload
            if (
                event.symbol != self.symbol
                or not isinstance(candle, Candle)
                or not candle.is_closed
                or event.received_at > timestamp
                or event.occurred_at > event.received_at
                or candle.closed_at - timedelta(milliseconds=1) > event.received_at
                or candle.closed_at - candle.opened_at != timedelta(minutes=1)
                or candle.opened_at.second != 0
                or candle.opened_at.microsecond != 0
            ):
                raise ValueError("reconciliation requires observed closed minute facts")
            original = identities.get(event.event_id) or self._original(event.event_id)
            if original is not None and not _same_fact(original, event):
                raise ValueError("reconciliation event identity conflicts")
            identities[event.event_id] = event
            previous = combined.get(candle.opened_at)
            if previous is not None and previous != candle:
                raise ValueError("reconciliation cannot overwrite a closed minute")
            combined[candle.opened_at] = candle
            provenance.setdefault(candle.opened_at, event)
            candidates.append(event)
            results.append(
                BufferUpdate(
                    event_id=event.event_id,
                    accepted=previous is None,
                    duplicate=previous is not None,
                )
            )
        if self._gap_since is not None:
            end = timestamp.replace(second=0, microsecond=0)
            start = max(
                self._gap_since.replace(second=0, microsecond=0), end - timedelta(minutes=120)
            )
            if start >= end:
                raise ValueError("gap's first completed minute is not available yet")
            while start < end:
                if start not in combined:
                    raise ValueError("reconciliation is missing completed minutes")
                start += timedelta(minutes=1)
        # All constraints are checked before any projection/clock/cache changes.
        self._candles = dict(sorted(combined.items())[-120:])
        self._candle_events = {key: provenance[key] for key in self._candles}
        for event in candidates:
            self._remember(event)
        self._observe_arrival(timestamp)
        self._gap, self._gap_since = False, None
        self._coverage_start = timestamp
        if self._candles:
            verified_end = min(timestamp, max(item.closed_at for item in self._candles.values()))
            self._max_event_at = max(self._max_event_at or verified_end, verified_end)
            self._flush()
        return tuple(results)

    def snapshot(self, symbol: str, as_of: datetime) -> MarketSnapshot:
        timestamp = utc_datetime(as_of)
        if required_identifier(symbol) != self.symbol:
            raise ValueError("snapshot scope does not match buffer")
        if self._published is not None:
            if timestamp < self._published.as_of:
                raise ValueError(
                    "buffer is a forward view; historical snapshots require archived evidence"
                )
            if timestamp == self._published.as_of:
                return self._published
        if self._observed_at is not None and timestamp < self._observed_at:
            raise ValueError("snapshot predates delivered observations")
        # Never reconstruct historical quotes from a newer mutable head.
        if any(
            item is not None and item.received_at > timestamp for item in (self._trade, self._book)
        ):
            raise ValueError("snapshot predates a delivered quote")
        quotes = tuple(item for item in (self._trade, self._book) if item is not None)
        latest_at = max((item.occurred_at for item in quotes), default=None)
        received_at = max((item.received_at for item in quotes), default=None)
        status = "warming" if not quotes else "ready"
        if quotes and (timestamp - latest_at).total_seconds() > 5:
            status = "stale"
        if self._gap:
            status = "gap"
        trade = self._trade
        if (
            trade is not None
            and self._book is not None
            and trade.occurred_at < self._book.occurred_at
        ):
            trade = None
        result = MarketSnapshot(
            symbol=self.symbol,
            as_of=timestamp,
            status=status,
            latest_quote_at=latest_at,
            latest_received_at=received_at,
            latest_trade=trade.payload if trade else None,
            book=self._book.payload if self._book else None,
            book_as_of=self._book.occurred_at if self._book else None,
            candles=tuple(item for item in self._candles.values() if item.closed_at <= timestamp),
        )
        self._published = result
        return result
