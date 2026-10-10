"""Read-only contract catalog and existing first-analysis facts."""

import logging

from fastapi import Depends, HTTPException, Query, Request

from agent_platform.domain.futures_chart import KlineInterval
from agent_platform.ports.session_analysis import HistoricalDataUnavailable

CATALOG_REASONS = frozenset(
    {
        "public_history_disabled",
        "rate_limited",
        "futures_public_unavailable",
        "invalid_futures_response",
        "futures_response_too_large",
        "futures_transport_or_data_error",
        "invalid_contract_catalog",
    }
)


def register_session_analysis_routes(app, guard):
    def service(request):
        value = request.app.state.services.initial_analysis
        if value is None:
            raise HTTPException(status_code=503, detail="首次分析模块尚未装配，请重启服务。")
        return value

    @app.get("/api/analysis/contracts", dependencies=[Depends(guard.require_read)])
    async def contracts(request: Request):
        try:
            rows = await service(request).contracts()
        except HistoricalDataUnavailable as error:
            code = str(error).split(":", 1)[0]
            logging.getLogger(__name__).warning(
                "USDT contract catalog unavailable: %s",
                code if code in CATALOG_REASONS else "unavailable",
            )
            raise HTTPException(
                status_code=503, detail="USDT 永续目录暂不可读；请启用公共行情并检查连接。"
            ) from None
        return {"market": "usdt_perpetual", "contracts": [r.model_dump(mode="json") for r in rows]}

    @app.get("/api/analysis/current", dependencies=[Depends(guard.require_read)])
    async def current(request: Request):
        return await service(request).public_view()

    @app.get("/api/analysis/chart", dependencies=[Depends(guard.require_read)])
    async def chart(
        request: Request, interval: KlineInterval = "1h", limit: int = Query(300, ge=1, le=499)
    ):
        try:
            return await service(request).chart_history(interval, limit)
        except ValueError:
            raise HTTPException(409, "当前合约会话已改变，请重新载入图表。") from None
        except (HistoricalDataUnavailable, TimeoutError):
            raise HTTPException(503, "合约 K 线暂不可读，稍后自动重试。") from None
