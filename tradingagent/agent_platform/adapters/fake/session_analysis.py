"""Explicit offline fixtures for the independent history and Flash contracts."""

from datetime import timedelta

from agent_platform.domain.futures_chart import ChartHistory, candle_boundary, shift_open
from agent_platform.domain.market import Candle
from agent_platform.domain.session_analysis import InitialAnalysisResult
from agent_platform.domain.session_market import HistoricalMarketData, PerpetualContract


class FakeHistoricalMarket:
    def __init__(self, clock):
        self.clock, self.calls = clock, 0

    async def catalog(self):
        return tuple(
            PerpetualContract(symbol=s, base_asset=s.removesuffix("USDT"))
            for s in ("BTCUSDT", "ETHUSDT", "1000SHIBUSDT")
        )

    async def history(self, target):
        self.calls += 1
        end = self.clock.utcnow().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=target.history_days)
        candles = tuple(
            Candle(
                symbol=target.symbol,
                opened_at=start + timedelta(hours=i),
                closed_at=start + timedelta(hours=i + 1, milliseconds=-1),
                open="2000",
                high="2010",
                low="1990",
                close="2001",
                volume="100",
                quote_volume="200000",
            )
            for i in range(target.history_days * 24)
        )
        return HistoricalMarketData(
            target=target,
            contract=PerpetualContract(
                symbol=target.symbol, base_asset=target.symbol.removesuffix("USDT")
            ),
            source="fake",
            captured_at=self.clock.utcnow(),
            requested_start=start,
            requested_end=end,
            candles=candles,
        )

    async def chart(self, request):
        end = candle_boundary(self.clock.utcnow(), request.interval)
        return ChartHistory(
            symbol=request.symbol,
            interval=request.interval,
            source="fake",
            captured_at=self.clock.utcnow(),
            candles=tuple(
                Candle(
                    symbol=request.symbol,
                    opened_at=shift_open(end, request.interval, i - request.limit),
                    closed_at=shift_open(end, request.interval, i - request.limit + 1)
                    - timedelta(milliseconds=1),
                    open="2000",
                    high="2010",
                    low="1990",
                    close="2001",
                    volume="100",
                    quote_volume="200000",
                )
                for i in range(request.limit)
            ),
        )


class FakeInitialAnalysis:
    def __init__(self):
        self.requests = []

    async def analyze(self, request):
        self.requests.append(request)
        return InitialAnalysisResult(
            request_id=request.request_id,
            history_hash=request.history.content_hash,
            source="fake",
            trend="uncertain",
            summary="离线接口验证，不代表真实模型观点。",
            evidence=("测试历史已收盘",),
            risks=("Fake结果不得用于交易",),
            watch_conditions=("接入真实模型后重新评估",),
        )
