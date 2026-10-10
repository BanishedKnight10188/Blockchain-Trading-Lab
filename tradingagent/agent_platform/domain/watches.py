"""Versioned, bounded watches over closed native Futures candles."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from .market import Candle
from .models import DomainModel, FiniteDecimal, Identifier, Revision, UtcDateTime

WatchInterval = Literal["1m", "5m"]
WatchState = Literal["ARMED", "TRIGGERED", "EXPIRED", "INVALIDATED", "CANCELLED"]
WATCH_LOOKBACK = 120
Metric = Literal[
    "candle.open",
    "candle.high",
    "candle.low",
    "candle.close",
    "volume_ratio_20",
    "ema_12",
    "ema_26",
    "atr_14",
]


def interval_delta(interval: WatchInterval) -> timedelta:
    if interval not in ("1m", "5m"):
        raise ValueError("watch interval must be 1m or 5m")
    return timedelta(minutes=1 if interval == "1m" else 5)


def fact_hash(value) -> str:
    """Lossless identity independent of Decimal display precision or context."""

    def canonical(item):
        if isinstance(item, Decimal):
            sign, digits, exponent = item.as_tuple()
            coefficient = "".join(str(digit) for digit in digits)
            trimmed = coefficient.rstrip("0")
            if not trimmed:
                return "0e0"
            return (
                ("-" if sign else "")
                + trimmed
                + "e"
                + str(exponent + len(coefficient) - len(trimmed))
            )
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, DomainModel):
            return canonical(item.model_dump())
        if isinstance(item, dict):
            return {key: canonical(part) for key, part in item.items()}
        if isinstance(item, (tuple, list)):
            return [canonical(part) for part in item]
        return item

    return sha256(
        json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Condition(DomainModel):
    metric: Metric
    op: Literal["GT", "GTE", "LT", "LTE", "BETWEEN"]
    value: FiniteDecimal | tuple[FiniteDecimal, FiniteDecimal]

    @model_validator(mode="after")
    def valid_operand(self) -> Self:
        if self.op == "BETWEEN":
            if not isinstance(self.value, tuple) or self.value[0] > self.value[1]:
                raise ValueError("BETWEEN requires an ordered inclusive pair")
        elif isinstance(self.value, tuple):
            raise ValueError("scalar comparison requires one decimal")
        return self


class ConditionGroup(DomainModel):
    logic: Literal["ALL", "ANY"]
    conditions: Annotated[tuple[Condition, ...], Field(min_length=1, max_length=8)]


class WatchDefinition(DomainModel):
    schema_version: Literal["watch-v1"] = "watch-v1"
    watch_id: Identifier
    definition_revision: Revision
    lane_id: Identifier
    session_id: Identifier
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9]{2,24}$")]
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    timeframe: WatchInterval
    price_kind: Literal["last_trade"] = "last_trade"
    hypothesis: Annotated[str, Field(min_length=1, max_length=4096)]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    created_at: UtcDateTime
    expires_at: UtcDateTime
    trigger: ConditionGroup
    invalidation: ConditionGroup | None = None
    max_triggers: Literal[1] = 1
    wake_agent: StrictBool = True
    parent_watch_id: Identifier | None = None

    @model_validator(mode="after")
    def bounded_lifetime(self) -> Self:
        if not timedelta(0) < self.expires_at - self.created_at <= timedelta(hours=24):
            raise ValueError("watch lifetime must be positive and at most 24 hours")
        return self

    @property
    def rule_hash(self) -> str:
        return fact_hash(self)

    @property
    def version(self) -> int:
        return self.definition_revision

    @property
    def interval(self) -> WatchInterval:
        return self.timeframe


class WatchRecord(DomainModel):
    definition: WatchDefinition
    revision: Revision = 1
    state: WatchState = "ARMED"
    last_candle_key: str | None = None
    last_input_hash: str | None = None
    evaluated_at: UtcDateTime | None = None
    reason: str | None = None
    event_id: str | None = None


class WatchFeatures(DomainModel):
    algorithm_version: Literal["watch-features-v1:decimal34:lookback120"] = (
        "watch-features-v1:decimal34:lookback120"
    )
    volume_ratio_20: FiniteDecimal | None = None
    ema_12: FiniteDecimal | None = None
    ema_26: FiniteDecimal | None = None
    atr_14: FiniteDecimal | None = None

    @property
    def unavailable_metrics(self) -> tuple[str, ...]:
        return tuple(
            k for k in ("volume_ratio_20", "ema_12", "ema_26", "atr_14") if getattr(self, k) is None
        )

    @property
    def availability(self) -> tuple[str, ...]:
        return tuple(
            k
            for k in ("volume_ratio_20", "ema_12", "ema_26", "atr_14")
            if getattr(self, k) is not None
        )


class WatchFrame(DomainModel):
    symbol: Identifier
    market: Literal["usdt_perpetual"] = "usdt_perpetual"
    interval: WatchInterval
    price_kind: Literal["last_trade"] = "last_trade"
    source: Literal["fake", "binance_futures_public"]
    data_version: Identifier
    received_at: UtcDateTime
    candles: Annotated[tuple[Candle, ...], Field(min_length=1, max_length=120)]
    quality: Literal["ready", "warming", "data_gap", "data_conflict"] = "ready"

    @model_validator(mode="after")
    def closed_point_in_time(self) -> Self:
        from .watch_features import validate_candles

        validate_candles(self.candles, self.interval)
        if any(c.symbol != self.symbol for c in self.candles):
            raise ValueError("watch frame contains another symbol")
        if self.occurred_at > self.received_at:
            raise ValueError("watch frame contains a future candle")
        return self

    @classmethod
    def from_candles(cls, **values) -> Self:
        values.setdefault("symbol", values["candles"][-1].symbol)
        return cls(**values)

    @property
    def occurred_at(self):
        return self.candles[-1].opened_at + interval_delta(self.interval)

    @property
    def candle_key(self) -> str:
        return f"{self.market}:{self.symbol}:{self.interval}:{self.occurred_at.isoformat()}"

    @property
    def features(self) -> WatchFeatures:
        from .watch_features import calculate_watch_features

        return calculate_watch_features(self.candles, self.interval)

    @property
    def effective_quality(self) -> str:
        if self.quality == "data_conflict":
            return "data_conflict"
        step = interval_delta(self.interval)
        if any(
            b.opened_at != a.opened_at + step
            for a, b in zip(self.candles, self.candles[1:], strict=False)
        ):
            return "data_gap"
        return self.quality

    @property
    def content_hash(self) -> str:
        facts = self.model_dump(exclude={"received_at", "quality"})
        # Reception and operational health do not change market fact identity.
        facts["features"] = self.features
        return fact_hash(facts)


class MetricValue(DomainModel):
    metric: Metric
    value: FiniteDecimal | None


class ConditionEvaluation(DomainModel):
    result: Literal["true", "false", "unavailable"]
    evaluated_values: tuple[MetricValue, ...]


class WatchEvaluation(DomainModel):
    evaluated_at: UtcDateTime
    next_state: WatchState
    emit_trigger: StrictBool = False
    reason: Identifier
    evaluated_values: tuple[MetricValue, ...] = ()
    input_hash: Identifier
