"""Historical facts and first analysis do not depend on exchange execution."""

from typing import Protocol

from agent_platform.domain.futures_chart import ChartHistory, ChartRequest
from agent_platform.domain.session_analysis import InitialAnalysisRequest, InitialAnalysisResult
from agent_platform.domain.session_market import (
    HistoricalMarketData,
    PerpetualContract,
    SessionAnalysisTarget,
)


class HistoricalDataUnavailable(RuntimeError):
    pass


class HistoricalMarketPort(Protocol):
    async def catalog(self) -> tuple[PerpetualContract, ...]: ...
    async def history(self, target: SessionAnalysisTarget) -> HistoricalMarketData: ...
    async def chart(self, request: ChartRequest) -> ChartHistory: ...


class InitialAnalysisPort(Protocol):
    # Production implementations MUST reserve and durably settle validated fees.
    async def analyze(self, request: InitialAnalysisRequest) -> InitialAnalysisResult: ...
