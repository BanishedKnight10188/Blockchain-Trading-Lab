from datetime import UTC, datetime, timedelta

import pytest

from agent_platform.domain.market import Candle
from agent_platform.domain.multiscale import SECONDS, CandleWindow, KlineBuffer, boundary

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def window(interval="1s", count=60, *, end=NOW, symbol="BTCUSDT"):
    seconds = SECONDS[interval]
    close_boundary = boundary(end, interval)
    return CandleWindow(
        symbol=symbol,
        interval=interval,
        source="fake",
        captured_at=end,
        requested_count=count,
        candles=tuple(
            Candle(
                symbol=symbol,
                opened_at=close_boundary - timedelta(seconds=(count - i) * seconds),
                closed_at=close_boundary
                - timedelta(seconds=(count - i - 1) * seconds, milliseconds=1),
                open="2000",
                high="2001",
                low="1999",
                close="2000",
                volume="10",
            )
            for i in range(count)
        ),
    )


def test_complete_eighty_bars_exact_decimal_columns_and_hash():
    short, fast = window("3m", 20), window()
    assert len(short.compact()) + len(fast.compact()) == 80
    assert fast.compact()[0][1:] == ["2000", "2001", "1999", "2000", "10"]
    assert len(fast.content_hash) == 64 and fast.complete


def test_wrong_identity_unfinished_and_gap_rejected():
    value = window().model_dump()
    with pytest.raises(ValueError):
        CandleWindow(**(value | {"symbol": "ETHUSDT"}))
    with pytest.raises(ValueError):
        CandleWindow(**(value | {"candles": value["candles"][:-2] + value["candles"][-1:]}))
    value["candles"][0]["is_closed"] = False
    with pytest.raises(ValueError):
        CandleWindow(**value)


def test_gap_and_disconnect_require_new_sixty_seconds_without_filling():
    buffer = KlineBuffer("BTCUSDT", "fake")
    buffer.accept(window())
    assert buffer.window("1s", 60, NOW).complete
    buffer.accept(window(count=1, end=NOW + timedelta(seconds=2)))
    assert not buffer.window("1s", 60, NOW + timedelta(seconds=2)).complete
    buffer.disconnect()
    assert not buffer.window("1s", 60, NOW + timedelta(seconds=2)).complete


def test_conflicting_closed_bar_is_rejected_and_old_data_is_not_fresh():
    buffer = KlineBuffer("BTCUSDT", "fake")
    value = window()
    buffer.accept(value)
    bad = value.model_dump()
    bad["candles"][-1]["close"] = "2001"
    with pytest.raises(ValueError):
        buffer.accept(CandleWindow(**bad))
    assert not buffer.window("1s", 60, NOW + timedelta(seconds=4)).fresh
