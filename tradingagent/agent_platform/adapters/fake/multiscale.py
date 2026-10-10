"""Deterministic offline evidence, never presented as Binance observations."""

from datetime import timedelta

from agent_platform.domain.background import BackgroundAnswer, BackgroundResult
from agent_platform.domain.market import Candle
from agent_platform.domain.multiscale import (
    BACKGROUND_WINDOWS,
    SECONDS,
    CandleWindow,
    KlineBuffer,
    boundary,
)


class OfflineKlines:
    connected, failure = False, None

    def __init__(self, clock):
        self.clock, self.buffer = clock, None

    async def start(self, symbol):
        if self.buffer is None or self.buffer.symbol != symbol:
            self.buffer = KlineBuffer(symbol, "fake")
        now = self.clock.utcnow()
        for interval, count in (*BACKGROUND_WINDOWS, ("3m", 20), ("1s", 60)):
            end, step = boundary(now, interval), timedelta(seconds=SECONDS[interval])
            bars = tuple(
                Candle(
                    symbol=symbol,
                    opened_at=end - step * (count - i),
                    closed_at=end - step * (count - i - 1) - timedelta(milliseconds=1),
                    open="2000",
                    high="2001",
                    low="1999",
                    close="2000",
                    volume="10",
                )
                for i in range(count)
            )
            self.buffer.seed(
                CandleWindow(
                    symbol=symbol,
                    source="fake",
                    interval=interval,
                    requested_count=count,
                    captured_at=now,
                    candles=bars,
                )
            )

    def ensure_prime(self):
        pass

    async def aclose(self):
        if self.buffer:
            self.buffer.disconnect()


class OfflineBackground:
    def __init__(self, clock, refresh_seconds=900):
        self.clock, self.refresh_seconds = clock, refresh_seconds

    async def analyze(self, request):
        return BackgroundResult(
            request=request,
            source="fake",
            model_id="offline-background-v1",
            generated_at=self.clock.utcnow(),
            expires_at=self.clock.utcnow() + timedelta(seconds=self.refresh_seconds),
            answer=BackgroundAnswer(
                layers=tuple(
                    {
                        "period": p,
                        "trend": "uncertain",
                        "summary": "离线固定数据验证，无真实市场判断。",
                        "risks": ("仅供离线验收。",),
                    }
                    for p in ("90d", "30d", "7d", "1d")
                )
            ),
        )
