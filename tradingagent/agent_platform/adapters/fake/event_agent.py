"""Explicit zero-cost WAIT model and closed candle fixtures for local development."""

import asyncio
from datetime import timedelta

from agent_platform.domain.costs import ModelUsage
from agent_platform.domain.event_agent import AgentFinalDecision, AgentTurnResponse
from agent_platform.domain.market import Candle
from agent_platform.domain.watches import WatchFrame


class OfflineEventAgentModel:
    paid = False

    def __init__(self, clock):
        self.clock, self.calls = clock, 0

    async def turn(self, request):
        self.calls += 1
        return AgentTurnResponse(
            request_id=request.request_id,
            final=AgentFinalDecision(
                action="WAIT", reason="离线模型默认等待；可用脚本模型验证工具与 Paper 闭环。"
            ),
            usage=ModelUsage(
                request_id=request.request_id,
                route_id=request.route.route_id,
                model_version=request.route.model_version,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd="0",
                actual_cost_usd="0",
                billing_status="confirmed",
                recorded_at=self.clock.utcnow(),
            ),
        )


class OfflineWatchData:
    def __init__(self, clock, price="2000"):
        self.clock, self.price = clock, price

    async def latest(self, symbol, interval):
        now = self.clock.utcnow()
        minutes = 1 if interval == "1m" else 5
        end = now.replace(minute=now.minute // minutes * minutes, second=0, microsecond=0)
        step = timedelta(minutes=minutes)
        bars = tuple(
            Candle(
                symbol=symbol,
                opened_at=end - step * (120 - i),
                closed_at=end - step * (119 - i) - timedelta(milliseconds=1),
                open=self.price,
                high=self.price,
                low=self.price,
                close=self.price,
                volume="100",
            )
            for i in range(120)
        )
        return WatchFrame(
            symbol=symbol,
            interval=interval,
            source="fake",
            data_version="offline-event-v1",
            received_at=now,
            candles=bars,
        )

    async def stream(self, symbol, interval, after):
        while True:
            frame = await self.latest(symbol, interval)
            if frame.candle_key != after:
                after = frame.candle_key
                yield frame
            await asyncio.sleep(0.5)
