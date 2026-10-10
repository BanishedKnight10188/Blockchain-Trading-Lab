"""Bounded event-time windows, immutable publications and explicit data gaps."""

import importlib
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from agent_platform.domain.market import MarketEvent
from tests.domain.test_decisions import NOW


def tick(identifier="t1", at=0, price="100", quantity="1", received=None):
    return MarketEvent(
        event_id=identifier,
        symbol="BTCUSDT",
        source="fake",
        occurred_at=NOW + timedelta(seconds=at),
        received_at=NOW + timedelta(seconds=at if received is None else received),
        time_quality="exchange",
        payload={
            "kind": "trade",
            "symbol": "BTCUSDT",
            "trade_id": identifier,
            "price": price,
            "quantity": quantity,
        },
    )


def book(identifier="b1", at=0, sequence=1):
    return MarketEvent(
        event_id=identifier,
        symbol="BTCUSDT",
        source="fake",
        occurred_at=NOW + timedelta(seconds=at),
        received_at=NOW + timedelta(seconds=at),
        time_quality="received",
        stream_sequence=sequence,
        payload={
            "kind": "book",
            "symbol": "BTCUSDT",
            "bid": "99",
            "ask": "101",
            "bid_quantity": "1",
            "ask_quantity": "1",
        },
    )


def minute(index=0, closed=True, identifier=None, *, at=None, close="100"):
    received = index * 60 + 60 if at is None else at
    return MarketEvent(
        event_id=identifier or f"m{index}-{closed}",
        symbol="BTCUSDT",
        source="fake",
        occurred_at=NOW + timedelta(seconds=received),
        received_at=NOW + timedelta(seconds=received),
        time_quality="exchange",
        payload={
            "kind": "candle",
            "symbol": "BTCUSDT",
            "opened_at": NOW + timedelta(minutes=index),
            "closed_at": NOW + timedelta(minutes=index + 1),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "1",
            "quote_volume": close,
            "is_closed": closed,
        },
    )


def buffer(**options):
    module = importlib.import_module("agent_platform.runtime.buffer")
    return module.MarketBuffer(**options)


def test_event_time_ohlc_volume_quote_volume_for_second_five_second_and_minute():
    window = buffer()
    for event in (
        tick(at=0, price="100"),
        tick("t2", 0.2, "110", "2"),
        tick("t3", 0.8, "90"),
        tick("t4", 63, "120"),
    ):
        assert window.append(event).accepted
    for interval in (1, 5, 60):
        bar = window.bars(interval)[0]
        assert bar.candle.open == 100
        assert bar.candle.close == 90
        assert bar.candle.high == 110
        assert bar.candle.low == 90
        assert bar.candle.volume == 4
        assert bar.candle.quote_volume == 410
        assert bar.trade_count == 3
        assert bar.candle.is_closed


def test_within_lateness_updates_pending_bar_in_event_order():
    window = buffer()
    window.append(tick("last", 0.8, "110", received=1))
    window.append(tick("first", 0.1, "100", received=1.1))
    window.append(tick("flush", 4, "120"))
    bar = window.bars(1)[0].candle
    assert (bar.open, bar.close, bar.volume) == (100, 110, 2)


def test_duplicate_with_a_later_reception_does_not_double_volume_or_refresh_quote():
    window = buffer()
    original = tick()
    window.append(original)
    repeat = MarketEvent.model_validate(
        {**original.model_dump(), "received_at": NOW + timedelta(seconds=4)}
    )
    result = window.append(repeat)
    assert result.duplicate and not result.accepted
    current = window.snapshot("BTCUSDT", NOW + timedelta(seconds=4))
    assert current.latest_quote_at == NOW
    assert current.latest_received_at == NOW
    window.append(tick("flush", 5))
    assert window.bars(1)[0].candle.volume == 1


def test_identity_conflict_fails_before_any_quote_or_volume_change():
    window = buffer()
    window.append(tick())
    with pytest.raises(ValueError):
        window.append(tick(price="101"))
    assert window.snapshot("BTCUSDT", NOW).latest_trade.price == 100


def test_watermark_too_late_event_records_gap_and_preserves_existing_facts():
    window = buffer()
    window.append(tick(at=0))
    window.append(tick("new", 10, "110"))
    late = window.append(tick("old", 1, "50", received=11))
    assert late.late and not late.accepted
    current = window.snapshot("BTCUSDT", NOW + timedelta(seconds=11))
    assert current.status == "gap"
    assert current.latest_trade.price == 110
    assert window.bars(1)[0].candle.close == 100


def test_unfinished_kline_does_not_swallow_final_closed_kline():
    window = buffer()
    window.append(minute(closed=False, at=30))
    assert not window.snapshot("BTCUSDT", NOW + timedelta(seconds=30)).candles
    window.append(minute())
    current = window.snapshot("BTCUSDT", NOW + timedelta(minutes=1))
    assert len(current.candles) == 1
    assert current.candles[0].quote_volume == 100


def test_a_closed_minute_is_immutable_even_when_a_new_event_id_claims_a_change():
    window = buffer()
    window.append(minute())
    with pytest.raises(ValueError):
        window.append(minute(identifier="correction", close="101"))
    assert window.snapshot("BTCUSDT", NOW + timedelta(minutes=1)).candles[0].close == 100


def test_published_same_time_snapshot_cannot_be_rewritten_by_backfill():
    window = buffer()
    window.append(tick(at=61))
    first = window.snapshot("BTCUSDT", NOW + timedelta(seconds=61))
    window.append(minute(at=62))
    assert window.snapshot("BTCUSDT", first.as_of) == first
    current = window.snapshot("BTCUSDT", NOW + timedelta(seconds=62))
    assert len(current.candles) == 1
    assert first.candles == ()
    with pytest.raises(ValueError):
        window.snapshot("BTCUSDT", NOW)


def test_future_events_or_inconsistent_final_candle_cannot_enter_current_context():
    window = buffer()
    for invalid in (tick(at=10, received=1), minute(at=30)):
        with pytest.raises(ValueError):
            window.append(invalid)
    assert window.snapshot("BTCUSDT", NOW).status == "warming"


def test_trade_and_book_have_independent_freshness():
    window = buffer()
    window.append(book())
    window.append(tick(at=6))
    current = window.snapshot("BTCUSDT", NOW + timedelta(seconds=6))
    assert current.status == "ready"
    assert current.latest_quote_at == NOW + timedelta(seconds=6)
    assert current.book_as_of == NOW
    later = window.snapshot("BTCUSDT", NOW + timedelta(seconds=12))
    assert later.status == "stale"


def test_book_sequence_cannot_regress_after_duplicate_cache_eviction():
    window = buffer(seen_capacity=1, seen_ttl_seconds=1)
    window.append(book("new-book", 1, 10))
    window.append(tick("t2", 5))
    result = window.append(book("old-book", 6, 9))
    assert result.late and not result.accepted
    assert window.snapshot("BTCUSDT", NOW + timedelta(seconds=6)).book_as_of == NOW + timedelta(
        seconds=1
    )


def test_long_run_bounds_all_windows_and_dedup_cache():
    window = buffer(seen_capacity=32, seen_ttl_seconds=10)
    for index in range(8000):
        window.append(tick(f"t{index}", index))
        if index % 60 == 59:
            window.append(minute(index // 60, at=index + 1))
    stats = window.stats
    assert stats.seen_events <= 11
    assert stats.pending_windows <= 12
    assert len(window.bars(1)) <= 300
    assert len(window.bars(5)) <= 60
    assert len(window.bars(60)) <= 120
    assert len(window.snapshot("BTCUSDT", NOW + timedelta(seconds=8000)).candles) <= 120


def test_pending_overflow_is_explicit_and_bounded():
    window = buffer(pending_capacity=3)
    window.append(tick("t1", 0.1))
    for index in range(1, 20):
        window.append(tick(f"burst-{index}", 0.2, received=0.3))
    assert window.stats.pending_events <= 3
    assert window.snapshot("BTCUSDT", NOW + timedelta(seconds=1)).status == "gap"


def test_full_pending_queue_does_not_prevent_a_newer_event_from_advancing_watermark():
    window = buffer(pending_capacity=1)
    window.append(tick(at=0))
    result = window.append(tick("new", 10))
    assert result.accepted
    assert len(window.bars(1)) == 1
    assert window.stats.pending_events == 1


def test_duplicate_only_receptions_expire_the_cache_without_refreshing_market_time():
    window = buffer(seen_ttl_seconds=1)
    window.append(book(at=0))
    repeated = book(at=10)
    result = window.append(repeated)
    assert result.duplicate
    assert window.stats.seen_events == 0
    assert window.snapshot("BTCUSDT", NOW + timedelta(seconds=10)).book_as_of == NOW


def test_new_historical_query_cannot_include_a_candle_delivered_after_its_capture_time():
    window = buffer()
    window.append(tick(at=0))
    window.append(minute(at=120))
    with pytest.raises(ValueError):
        window.snapshot("BTCUSDT", NOW + timedelta(seconds=60))


def test_only_one_minute_authoritative_candles_enter_the_minute_window():
    window = buffer()
    raw = minute().model_dump()
    raw["payload"]["opened_at"] = NOW + timedelta(seconds=30)
    with pytest.raises(ValueError):
        window.append(MarketEvent.model_validate(raw))


def test_reconciliation_fills_closed_minutes_and_keeps_old_publication_frozen():
    window = buffer()
    window.append(book(at=0))
    window.mark_gap("disconnected", NOW + timedelta(seconds=10))
    gap = window.snapshot("BTCUSDT", NOW + timedelta(seconds=10))
    window.append(book("after-reconnect", 61, 2))
    result = window.reconcile((minute(at=61),), NOW + timedelta(seconds=61))
    assert result[0].accepted
    assert window.snapshot("BTCUSDT", gap.as_of) == gap
    restored = window.snapshot("BTCUSDT", NOW + timedelta(seconds=61))
    assert restored.status == "ready"
    assert len(restored.candles) == 1


def test_incomplete_or_conflicting_reconciliation_does_not_partly_update_projection():
    window = buffer()
    window.append(minute())
    window.mark_gap("disconnected", NOW + timedelta(seconds=60))
    window.append(book(at=121))
    for events in ((), (minute(1, at=121), minute(0, at=121, close="101"))):
        with pytest.raises(ValueError):
            window.reconcile(events, NOW + timedelta(seconds=121))
    current = window.snapshot("BTCUSDT", NOW + timedelta(seconds=121))
    assert current.status == "gap"
    assert len(current.candles) == 1


def test_reconciliation_cannot_clear_a_gap_before_its_first_minute_is_complete():
    window = buffer()
    window.append(book())
    window.mark_gap("disconnected", NOW + timedelta(seconds=10))
    with pytest.raises(ValueError):
        window.reconcile((), NOW + timedelta(seconds=30))


def test_partial_start_and_gap_windows_are_not_advertised_as_complete_trade_coverage():
    window = buffer()
    window.append(tick(at=0.5))
    window.append(tick("flush", 5))
    assert not window.bars(1)[0].complete
    window.mark_gap("disconnected", NOW + timedelta(seconds=5))
    window.append(tick("gap-trade", 6))
    window.append(tick("flush-gap", 10))
    assert not window.bars(1)[-1].complete


def test_no_zero_volume_windows_are_fabricated_for_missing_seconds():
    window = buffer()
    window.append(tick(at=0))
    window.append(tick("new", 10))
    assert len(window.bars(1)) == 1
    assert window.bars(1)[0].candle.opened_at == NOW


def test_precise_aggregation_is_independent_of_ambient_context():
    def aggregate():
        window = buffer()
        window.append(tick(at=0, price="100.123456789", quantity="0.123456789"))
        window.append(tick("flush", 4))
        return window.bars(1)[0].candle.quote_volume

    original = aggregate()
    with localcontext() as context:
        context.prec = 3
        assert aggregate() == original
    assert original == Decimal("12.360920478750190521")


def test_reconciliation_rejects_conflicting_identity_inside_the_batch_atomically():
    window = buffer()
    with pytest.raises(ValueError):
        window.reconcile(
            (minute(0, identifier="shared", at=121), minute(1, identifier="shared", at=121)),
            NOW + timedelta(seconds=121),
        )
    assert window.snapshot("BTCUSDT", NOW + timedelta(seconds=121)).candles == ()
    assert window.stats.seen_events == 0


@pytest.mark.parametrize("retained", ["pending", "quote", "minute"])
def test_reconciliation_checks_retained_identity_after_seen_cache_eviction(retained):
    window = buffer(seen_capacity=1)
    if retained == "minute":
        window.append(minute(identifier="shared"))
        window.append(book(at=61))
    else:
        window.append(tick("shared", 0))
        window.append(minute(at=1, closed=False))
        if retained == "quote":
            window.append(minute(at=10, closed=False, identifier="later-partial"))
            assert window.stats.pending_events == 0
    with pytest.raises(ValueError):
        window.reconcile((minute(1, identifier="shared", at=121),), NOW + timedelta(seconds=121))


def test_append_checks_retained_minute_identity_after_seen_cache_eviction():
    window = buffer(seen_capacity=1)
    window.append(minute(identifier="shared"))
    window.append(book(at=61))
    with pytest.raises(ValueError):
        window.append(tick("shared", 62))


def test_rejected_late_event_advances_observation_clock_without_refreshing_quote():
    window = buffer()
    window.append(tick(at=10))
    window.append(tick("late", 0, received=11))
    with pytest.raises(ValueError):
        window.snapshot("BTCUSDT", NOW + timedelta(seconds=10.5))
    assert window.snapshot(
        "BTCUSDT", NOW + timedelta(seconds=11)
    ).latest_quote_at == NOW + timedelta(seconds=10)


def test_final_candle_inclusive_millisecond_boundary_is_accepted_but_not_published_early():
    window = buffer()
    assert window.append(minute(at=59.999)).accepted
    assert not window.snapshot("BTCUSDT", NOW + timedelta(seconds=59.999)).candles
    assert len(window.snapshot("BTCUSDT", NOW + timedelta(seconds=60)).candles) == 1


def test_gap_recovery_must_include_the_actual_missing_candle_window():
    window = buffer()
    window.append(minute())
    window.append(book(at=170))
    raw = minute(1, at=120).model_dump()
    raw["received_at"] = NOW + timedelta(seconds=171)
    assert window.append(MarketEvent.model_validate(raw)).late
    window.append(book("new", 181, 2))
    with pytest.raises(ValueError):
        window.reconcile((minute(2, at=181),), NOW + timedelta(seconds=181))
    assert window.snapshot("BTCUSDT", NOW + timedelta(seconds=181)).status == "gap"
    window.reconcile((minute(1, at=182), minute(2, at=182)), NOW + timedelta(seconds=182))
    assert window.snapshot("BTCUSDT", NOW + timedelta(seconds=182)).status == "ready"


def test_reconciliation_advances_event_watermark_to_verified_completed_minutes():
    window = buffer()
    window.append(book())
    window.mark_gap("disconnected", NOW + timedelta(seconds=10))
    window.reconcile((minute(at=61),), NOW + timedelta(seconds=61))
    assert window.watermark >= NOW + timedelta(seconds=58)
    assert window.append(tick("delayed", 10, received=62)).late


def test_repeated_reconciliation_preserves_cache_ttl_order():
    window = buffer(seen_ttl_seconds=2)
    window.append(minute(identifier="minute-a"))
    window.append(book("book-b", 60.1))
    repeated = MarketEvent.model_validate(
        {**minute(identifier="minute-a").model_dump(), "received_at": NOW + timedelta(seconds=61)}
    )
    window.reconcile((repeated,), NOW + timedelta(seconds=61))
    window.append(book("book-c", 62.2, 2))
    assert window.stats.seen_events == 2


@pytest.mark.parametrize(
    "options",
    [
        {"lateness_seconds": -1},
        {"seen_capacity": True},
        {"seen_capacity": 0},
        {"seen_ttl_seconds": 0},
        {"pending_capacity": 0},
    ],
)
def test_invalid_buffer_bounds_are_rejected(options):
    with pytest.raises(ValueError):
        buffer(**options)
