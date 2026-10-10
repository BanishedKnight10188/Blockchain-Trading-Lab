"""Observable rule semantics; no model, wall clock or executable expressions."""

import importlib
from datetime import timedelta

import pytest

from tests.fixtures.watch_cases import NOW, candles, definition, frame, record


def evaluate(watch=None, snapshot=None, now=NOW):
    return importlib.import_module("agent_platform.domain.watch_rules").evaluate_watch(
        watch or record(), snapshot or frame(), now
    )


def test_same_candle_semantics_and_evidence():
    result = evaluate()
    assert result.next_state == "TRIGGERED" and result.emit_trigger
    assert result.evaluated_values
    assert result.input_hash == frame().content_hash


def test_invalidation_precedes_trigger():
    result = evaluate(
        record(
            invalidation={
                "logic": "ALL",
                "conditions": [{"metric": "candle.close", "op": "GTE", "value": "105"}],
            }
        )
    )
    assert result.next_state == "INVALIDATED" and not result.emit_trigger


@pytest.mark.parametrize("state", ["TRIGGERED", "CANCELLED", "EXPIRED", "INVALIDATED"])
def test_terminal_watch_does_not_rearm(state):
    result = evaluate(record().model_copy(update={"state": state}))
    assert result.next_state == state and not result.emit_trigger


def test_expiry_is_half_open_and_precedes_missing_data():
    watch = record(expires_at=NOW)
    result = evaluate(watch, frame(quality="warming"))
    assert result.next_state == "EXPIRED" and not result.emit_trigger


def test_gap_frame_does_not_trigger_or_invalidate():
    bars = candles(26)
    result = evaluate(snapshot=frame((*bars[:20], *bars[21:])))
    assert result.next_state == "ARMED" and result.reason == "data_gap"


def test_only_referenced_features_need_warmup():
    assert evaluate(snapshot=frame(candles(1))).emit_trigger
    watch = record(
        trigger={
            "logic": "ALL",
            "conditions": [{"metric": "ema_26", "op": "GT", "value": "100"}],
        }
    )
    result = evaluate(watch, frame(candles(21)))
    assert not result.emit_trigger and result.reason == "metric_unavailable"


def test_history_before_creation_only_warms_up():
    assert not evaluate(record(created_at=NOW, expires_at=NOW + timedelta(hours=1))).emit_trigger


def test_unclosed_or_future_frame_rejected():
    with pytest.raises(ValueError):
        frame((candles(1)[0].model_copy(update={"is_closed": False}),))
    with pytest.raises(ValueError):
        frame(received_at=NOW - timedelta(minutes=1))


def test_hash_ignores_reception_time_but_captures_changed_facts():
    assert frame().content_hash == frame(received_at=NOW + timedelta(seconds=1)).content_hash
    assert frame().content_hash != frame(candles(volume="101")).content_hash


@pytest.mark.parametrize(
    "condition",
    [
        {"metric": "exec", "op": "GT", "value": "1"},
        {"metric": "candle.close", "op": "GT", "value": "NaN"},
        {"metric": "candle.close", "op": "GT", "value": "Infinity"},
        {"metric": "candle.close", "op": "GT", "value": 1.1},
        {"metric": "candle.close", "op": "GT", "value": True},
        {"metric": "candle.close", "op": "BETWEEN", "value": ["2", "1"]},
        {"metric": "candle.close", "op": "GT", "value": ["1", "2"]},
        {"metric": "candle.close", "op": "GT", "value": "1", "code": "print(1)"},
    ],
)
def test_unknown_metric_and_nan_rejected(condition):
    with pytest.raises(ValueError):
        definition(trigger={"logic": "ALL", "conditions": [condition]})


def test_cross_bar_configuration_is_not_silently_compiled():
    with pytest.raises(ValueError):
        definition(trigger={"logic": "SEQUENCE", "conditions": []})
    with pytest.raises(ValueError):
        definition(expires_at=NOW + timedelta(days=2))


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("GT", "105", False),
        ("GTE", "105", True),
        ("LT", "105", False),
        ("LTE", "105", True),
        ("BETWEEN", ["105", "105"], True),
    ],
)
def test_comparison_boundaries(op, value, expected):
    watch = record(
        trigger={
            "logic": "ALL",
            "conditions": [{"metric": "candle.close", "op": op, "value": value}],
        }
    )
    assert evaluate(watch).emit_trigger is expected


def test_any_requires_one_known_true_value():
    watch = record(
        trigger={
            "logic": "ANY",
            "conditions": [
                {"metric": "ema_26", "op": "GT", "value": "999"},
                {"metric": "candle.close", "op": "GTE", "value": "105"},
            ],
        }
    )
    assert evaluate(watch, frame(candles(1))).emit_trigger


def test_scope_mismatch_and_decimal_roundtrip():
    with pytest.raises(ValueError):
        evaluate(record(symbol="ETHUSDT"))
    watch = definition()
    assert type(watch).model_validate_json(watch.model_dump_json()) == watch
