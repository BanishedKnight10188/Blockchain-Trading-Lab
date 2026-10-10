"""Run an offline Watch -> durable event -> lease -> replay demonstration."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agent_platform.adapters.sqlite.agent_events import SqliteAgentEventStore
from agent_platform.adapters.sqlite.watches import SqliteWatchStore
from agent_platform.application.watches import WatchService
from agent_platform.domain.market import Candle
from agent_platform.domain.watches import WatchDefinition, WatchFrame
from agent_platform.replay.watches import replay_watches

PROJECT_ROOT = Path(__file__).resolve().parents[1]


async def main():
    now = datetime(2026, 10, 9, 6, 0, tzinfo=UTC)
    candles = tuple(
        Candle(
            symbol="BTCUSDT",
            opened_at=now - timedelta(minutes=26 - index),
            closed_at=now - timedelta(minutes=25 - index, milliseconds=1),
            open="100",
            high="110",
            low="90",
            close="105",
            volume="100",
        )
        for index in range(26)
    )
    definition = WatchDefinition(
        watch_id="offline-watch",
        definition_revision=1,
        lane_id="event-demo",
        session_id="event-demo-session",
        symbol="BTCUSDT",
        timeframe="1m",
        hypothesis="Closed bar reclaimed 104 after touching the 90–100 zone",
        created_at=now - timedelta(minutes=2),
        expires_at=now + timedelta(hours=1),
        trigger={
            "logic": "ALL",
            "conditions": [
                {"metric": "candle.low", "op": "BETWEEN", "value": ["90", "100"]},
                {"metric": "candle.close", "op": "GT", "value": "104"},
            ],
        },
        invalidation={
            "logic": "ALL",
            "conditions": [
                {"metric": "candle.close", "op": "LT", "value": "80"},
            ],
        },
    )
    frame = WatchFrame.from_candles(
        candles=candles,
        interval="1m",
        source="fake",
        data_version="watch-data-v1",
        received_at=now,
    )
    # A unique workspace path always avoids existing JEV or user databases.
    database = (
        PROJECT_ROOT / "output" / "verification" / "event-watch-demo" / uuid4().hex / "core.sqlite"
    )
    watches, events = SqliteWatchStore(database), SqliteAgentEventStore(database)
    await watches.initialize()
    await watches.create(definition)
    service = WatchService(watches, lane_id="event-demo")
    await service.process(frame, now)
    await service.process(frame, now)  # duplicate cannot generate a second event
    lease = await events.claim("event-demo", now, 120)
    if lease is None:
        raise RuntimeError("offline trigger was not claimable")
    await events.complete(lease, "offline-run", now)
    watch = await watches.get(definition.watch_id)
    replay = replay_watches((definition,), (frame, frame))
    if (
        replay.event_ids != (watch.event_id,)
        or dict(replay.final_states)[definition.watch_id] != watch.state
    ):
        raise RuntimeError("offline live/replay mismatch")
    print(
        json.dumps(
            {
                "database": str(database),
                "watch_state": watch.state,
                "event_id": watch.event_id,
                "delivery": (await events.delivery(watch.event_id)).status,
                "replay_equal": True,
                "input_hash": frame.content_hash,
                "metrics": frame.features.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
