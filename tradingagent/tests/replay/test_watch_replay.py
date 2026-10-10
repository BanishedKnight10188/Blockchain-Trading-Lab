"""Replay runs the same point-in-time evaluator and deterministic identity."""

import importlib
from datetime import timedelta

import pytest

from tests.adapters.test_watch_store import stores
from tests.fixtures.watch_cases import NOW, candles, definition, frame


@pytest.mark.asyncio
async def test_live_and_replay_equal(tmp_path):
    from agent_platform.application.watches import WatchService

    definitions = (definition(), definition(watch_id="other", lane_id="lane-2"))
    store, _ = await stores(tmp_path / "core.sqlite")
    for watch in definitions:
        await store.create(watch)
    app = WatchService(store, lane_id="lane-1")
    inputs = (
        frame(candles(end=NOW - timedelta(minutes=2)), received_at=NOW - timedelta(minutes=2)),
        frame(),
        frame(),
    )
    for snapshot in inputs:
        await app.process(snapshot, snapshot.received_at)
    replay = importlib.import_module("agent_platform.replay.watches").replay_watches(
        definitions, inputs, lane_id="lane-1"
    )
    watch = await store.get("watch-1")
    assert replay.event_ids == (watch.event_id,)
    assert dict(replay.final_states)["watch-1"] == watch.state
    assert dict(replay.final_states)["other"] == (await store.get("other")).state
    assert len(replay.input_hashes) == 3 and len(replay.rule_hashes) == 2


def test_replay_conflict_and_chronological_clock():
    replay = importlib.import_module("agent_platform.replay.watches").replay_watches
    no_trigger = definition(
        trigger={
            "logic": "ALL",
            "conditions": [{"metric": "candle.close", "op": "GT", "value": "109"}],
        }
    )
    changed = frame(tuple(c.model_copy(update={"volume": c.volume + 1}) for c in candles()))
    report = replay((no_trigger,), (frame(), changed))
    assert dict(report.final_states)["watch-1"] == "ARMED" and report.conflicts
    with pytest.raises(ValueError, match="chronological"):
        replay(
            (definition(),),
            (
                frame(),
                frame(
                    candles(end=NOW - timedelta(minutes=1)), received_at=NOW - timedelta(minutes=1)
                ),
            ),
        )


def test_replay_accepts_equal_decimal_facts():
    from decimal import Decimal

    replay = importlib.import_module("agent_platform.replay.watches").replay_watches
    watch = definition(
        trigger={
            "logic": "ALL",
            "conditions": [
                {"metric": "candle.close", "op": "GT", "value": "109"},
            ],
        }
    )
    scaled = frame(tuple(c.model_copy(update={"close": Decimal("105.00")}) for c in candles()))
    report = replay((watch,), (frame(), scaled))
    assert not report.conflicts and report.input_hashes[0] == report.input_hashes[1]
