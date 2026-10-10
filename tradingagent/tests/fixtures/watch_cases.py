"""Hand-checkable closed-bar fixtures for the independent watch lane."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agent_platform.domain.market import Candle

NOW = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)


def candles(count=26, *, interval="1m", end=NOW, volume="100"):
    step = timedelta(minutes=1 if interval == "1m" else 5)
    return tuple(
        Candle(
            symbol="BTCUSDT",
            opened_at=end - step * (count - index),
            closed_at=end - step * (count - index - 1) - timedelta(milliseconds=1),
            open="100",
            high="110",
            low="90",
            close="105",
            volume=volume,
        )
        for index in range(count)
    )


def definition(**changes):
    from agent_platform.domain.watches import WatchDefinition

    data = dict(
        watch_id="watch-1",
        definition_revision=1,
        lane_id="lane-1",
        session_id="session-watch-1",
        symbol="BTCUSDT",
        timeframe="1m",
        hypothesis="Same closed bar retest and reclaim",
        created_at=NOW - timedelta(minutes=2),
        expires_at=NOW + timedelta(hours=1),
        trigger={
            "logic": "ALL",
            "conditions": [
                {"metric": "candle.low", "op": "BETWEEN", "value": ["90", "100"]},
                {"metric": "candle.close", "op": "GT", "value": "104"},
            ],
        },
        invalidation={
            "logic": "ALL",
            "conditions": [{"metric": "candle.close", "op": "LT", "value": "80"}],
        },
    )
    return WatchDefinition.model_validate(data | changes)


def frame(bars=None, *, interval="1m", received_at=NOW, quality="ready"):
    from agent_platform.domain.watches import WatchFrame

    return WatchFrame.from_candles(
        candles=bars if bars is not None else candles(interval=interval),
        interval=interval,
        source="fake",
        data_version="watch-data-v1",
        received_at=received_at,
        quality=quality,
    )


def record(**changes):
    from agent_platform.domain.watches import WatchRecord

    return WatchRecord(definition=definition(**changes), revision=1)


def last_volume(bars, volume):
    return (*bars[:-1], bars[-1].model_copy(update={"volume": Decimal(volume)}))
