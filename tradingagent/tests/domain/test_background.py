from datetime import timedelta

import pytest
from pydantic import ValidationError

from agent_platform.domain.background import (
    BackgroundAnswer,
    BackgroundRequest,
    BackgroundResult,
    MultiScaleSnapshot,
)
from agent_platform.domain.multiscale import BACKGROUND_WINDOWS, KlineBuffer
from tests.domain.test_multiscale import NOW, window


def request():
    return BackgroundRequest(
        request_id="bg1",
        session_id="s1",
        symbol="BTCUSDT",
        style_revision=1,
        style_strength=50,
        created_at=NOW,
        deadline=NOW + timedelta(seconds=15),
        windows=tuple(window(interval=i, count=n) for i, n in BACKGROUND_WINDOWS),
    )


def answer():
    return BackgroundAnswer(
        layers=tuple(
            {
                "period": p,
                "trend": "uncertain",
                "summary": "Recorded evidence.",
                "risks": ("No predictive certainty.",),
            }
            for p in ("90d", "30d", "7d", "1d")
        )
    )


def result():
    r = request()
    return BackgroundResult(
        request=r,
        answer=answer(),
        source="fake",
        model_id="offline-background-v1",
        generated_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )


def test_snapshot_requires_exact_short_windows_and_current_binding():
    s = MultiScaleSnapshot(
        background=result(), short=window("3m", 20), fast=window("1s", 60), captured_at=NOW
    )
    assert s.ready_for("s1", 1, "BTCUSDT", NOW)
    assert not s.ready_for("s2", 1, "BTCUSDT", NOW)
    assert not s.ready_for("s1", 2, "BTCUSDT", NOW)
    assert not s.ready_for("s1", 1, "BTCUSDT", NOW + timedelta(minutes=16))
    data = s.context_data()
    assert len(data["short_3m"]["candles"]) + len(data["fast_1s"]["candles"]) == 80
    assert len(data["background"]["layers"]) == 4
    facts = request().context_data()["windows"][0]["features"]
    assert facts["base_volume_sum"] == "900"
    assert facts["change_from_first_open_percent"] == "0"
    assert facts["last_position_in_range"] == "0.5"
    with pytest.raises(ValidationError):
        s.model_copy(update={"fast": window("1s", 59)}).__class__.model_validate_json(
            s.model_copy(update={"fast": window("1s", 59)}).model_dump_json()
        )


def test_history_preload_fills_prefix_without_overwriting_live_evidence():
    all_bars = window("3m", 20)
    buf = KlineBuffer("BTCUSDT", "fake")
    buf.accept(all_bars.model_copy(update={"requested_count": 1, "candles": all_bars.candles[-1:]}))
    buf.seed(all_bars)
    assert buf.window("3m", 20, NOW).complete
