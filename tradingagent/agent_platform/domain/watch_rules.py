"""Pure three-valued comparisons; no expression interpreter or side effects."""

from datetime import datetime

from .common import utc_datetime
from .watches import (
    ConditionEvaluation,
    ConditionGroup,
    MetricValue,
    WatchEvaluation,
    WatchFrame,
    WatchRecord,
)


def evaluate_conditions(conditions: ConditionGroup, frame: WatchFrame) -> ConditionEvaluation:
    features = frame.features
    values = []
    outcomes = []
    for condition in conditions.conditions:
        value = (
            getattr(frame.candles[-1], condition.metric.split(".")[1])
            if condition.metric.startswith("candle.")
            else getattr(features, condition.metric)
        )
        values.append(MetricValue(metric=condition.metric, value=value))
        if value is None:
            outcomes.append(None)
        elif condition.op == "BETWEEN":
            outcomes.append(condition.value[0] <= value <= condition.value[1])
        else:
            operand = condition.value
            outcomes.append(
                {
                    "GT": value > operand,
                    "GTE": value >= operand,
                    "LT": value < operand,
                    "LTE": value <= operand,
                }[condition.op]
            )
    if conditions.logic == "ALL":
        outcome = False if False in outcomes else None if None in outcomes else True
    else:
        outcome = True if True in outcomes else None if None in outcomes else False
    return ConditionEvaluation(
        result="unavailable" if outcome is None else str(outcome).lower(),
        evaluated_values=tuple(values),
    )


def evaluate_watch(watch: WatchRecord, frame: WatchFrame, now: datetime) -> WatchEvaluation:
    now = utc_datetime(now)
    definition = watch.definition
    if (definition.symbol, definition.market, definition.interval, definition.price_kind) != (
        frame.symbol,
        frame.market,
        frame.interval,
        frame.price_kind,
    ):
        raise ValueError("watch/frame scope mismatch")
    if frame.occurred_at > now:
        raise ValueError("evaluation cannot use future data")
    common = {"input_hash": frame.content_hash, "evaluated_at": now}
    if watch.state != "ARMED":
        return WatchEvaluation(next_state=watch.state, reason="terminal", **common)
    if now >= definition.expires_at:
        return WatchEvaluation(next_state="EXPIRED", reason="expired", **common)
    if frame.candles[-1].opened_at < definition.created_at:
        return WatchEvaluation(next_state="ARMED", reason="before_creation", **common)
    if frame.effective_quality != "ready":
        return WatchEvaluation(next_state="ARMED", reason=frame.effective_quality, **common)
    values = ()
    if definition.invalidation is not None:
        invalidation = evaluate_conditions(definition.invalidation, frame)
        values = invalidation.evaluated_values
        if invalidation.result == "unavailable":
            return WatchEvaluation(
                next_state="ARMED", reason="metric_unavailable", evaluated_values=values, **common
            )
        if invalidation.result == "true":
            return WatchEvaluation(
                next_state="INVALIDATED", reason="invalidation", evaluated_values=values, **common
            )
    trigger = evaluate_conditions(definition.trigger, frame)
    values += trigger.evaluated_values
    return WatchEvaluation(
        next_state="TRIGGERED" if trigger.result == "true" else "ARMED",
        emit_trigger=trigger.result == "true",
        reason={
            "true": "trigger",
            "false": "conditions_false",
            "unavailable": "metric_unavailable",
        }[trigger.result],
        evaluated_values=values,
        **common,
    )
