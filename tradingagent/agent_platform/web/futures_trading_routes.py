"""Protected controls for the generic futures run, initially backed by virtual funds."""

from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from agent_platform.application.futures_trading import TradingGuard
from agent_platform.domain.models import Identifier, Revision
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.futures_paper import FuturesPaperGuardConflict
from agent_platform.ports.sessions import PersistenceUnavailable
from agent_platform.ports.trading_execution import ExecutionRejected
from agent_platform.web.feedback_routes import Confirmation


class FuturesConfigure(Confirmation):
    limits: TradingLimits
    policy: TradingPolicy
    session_id: Identifier
    style_revision: Revision


class FuturesAction(Confirmation):
    account_ref: Identifier
    expected_revision: Revision
    style_revision: Revision
    trader_revision: Revision


def action_failure_code(error):
    if isinstance(error, FuturesMarketUnavailable):
        return "market_unavailable"
    if isinstance(error, FuturesPaperGuardConflict):
        return "account_permission_changed"
    if isinstance(error, (TradingGuard, ExecutionRejected)):
        reason = str(error)
        if reason in {
            "runtime_not_owned",
            "selection_changed",
            "session_not_running",
            "run_unconfigured",
            "source_changed",
            "execution_unresolved",
            "funding_publication_pending",
            "funding_catchup_pending",
            "funding_clock_regressed",
            "maintenance_quote_unavailable",
            "funding_window_unavailable",
        }:
            return reason
        return "execution_guard"
    return {
        "mark evidence is older than or conflicts with its watermark": "quote_watermark_conflict",
        "simulation requires fresh nonfuture evidence": "quote_time_invalid",
        "simulation evidence identity or chronology conflicts": "quote_identity_or_time_invalid",
        "paper model trial expired or has not started": "model_policy_expired",
        "paper model is read-only": "model_read_only",
    }.get(str(error), "invalid_trading_evidence")


def register_futures_trading_routes(app, guard):
    def service(request):
        value = request.app.state.services.futures_trading
        if value is None:
            raise HTTPException(
                503,
                "合约操盘未装配；使用 --paper --paper-mock 验证框架，或有效的真实 JEV 费用配置。",
            )
        return value

    @app.get("/api/futures-trading", dependencies=[Depends(guard.require_read)])
    async def current(request: Request):
        value = request.app.state.services.futures_trading
        if value is not None:
            return await value.public_view()
        session = await request.app.state.services.sessions.store.active()
        return {
            "enabled": False,
            "environment": "paper",
            "real_orders_enabled": False,
            "paid_models_enabled": False,
            "account": None,
            "cycles": [],
            "operations": [],
            "analysis_target": session.analysis_target.model_dump(mode="json") if session else None,
        }

    @app.post("/api/futures-trading/configure", dependencies=[Depends(guard.require_write)])
    async def configure(request: Request, selection: FuturesConfigure):
        value = service(request)
        try:
            await value.configure(
                selection.limits,
                selection.policy,
                session_id=selection.session_id,
                style_revision=selection.style_revision,
            )
        except (
            TradingGuard,
            FuturesPaperGuardConflict,
            FuturesMarketUnavailable,
            ExecutionRejected,
            ValueError,
        ):
            raise HTTPException(
                409,
                "配置未就绪：请核对合约会话、风格、自动 + Paper 开关与行情。"
                "本会话资金与策略不能重置。",
            ) from None
        return await value.public_view()

    async def action(request, selection, name):
        value = service(request)
        try:
            if name == "start":
                await value.start(
                    account_ref=selection.account_ref,
                    expected_revision=selection.expected_revision,
                    style_revision=selection.style_revision,
                    trader_revision=selection.trader_revision,
                )
            else:
                await value.pause(
                    account_ref=selection.account_ref, expected_revision=selection.expected_revision
                )
        except (
            TradingGuard,
            FuturesPaperGuardConflict,
            FuturesMarketUnavailable,
            ExecutionRejected,
            ValueError,
        ) as error:
            raise HTTPException(
                409,
                "操作未就绪：行情暂不可用，或会话、账户、风格、操盘版本已变化；"
                "真实模型还需要有效的本机预算。原因：" + action_failure_code(error),
            ) from None
        return await value.public_view()

    @app.post("/api/futures-trading/start", dependencies=[Depends(guard.require_write)])
    async def start(request: Request, selection: FuturesAction):
        return await action(request, selection, "start")

    @app.post("/api/futures-trading/pause", dependencies=[Depends(guard.require_write)])
    async def pause(request: Request, selection: FuturesAction):
        return await action(request, selection, "pause")

    async def archive_scope(request):
        value = service(request)
        session = await value.sessions.active()
        if (
            session is None
            or session.analysis_target.market != "usdt_perpetual"
            or value.archive is None
        ):
            raise HTTPException(409, "请先选择合约会话并建立虚拟钱包。")
        return value.archive, value.scope(session).account_ref

    @app.get("/api/futures-trading/archive", dependencies=[Depends(guard.require_read)])
    async def archive_page(
        request: Request,
        after: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        through: int | None = Query(None, ge=0),
    ):
        archive, account_ref = await archive_scope(request)
        try:
            page = await archive.page(account_ref, after=after, limit=limit, through=through)
        except (ValueError, LookupError, PersistenceUnavailable):
            raise HTTPException(409, "档案未就绪或完整性校验失败，请保留数据库并核对。") from None
        return page.model_dump(mode="json")

    @app.get("/api/futures-trading/archive/export", dependencies=[Depends(guard.require_read)])
    async def archive_export(request: Request, format: Literal["jsonl", "csv"] = "jsonl"):
        archive, account_ref = await archive_scope(request)
        try:
            output = await archive.export(account_ref, format=format)
        except (ValueError, LookupError, PersistenceUnavailable):
            raise HTTPException(409, "无法导出：档案未就绪或完整性校验失败。") from None

        def chunks():
            try:
                while data := output.read(65536):
                    yield data
            finally:
                output.close()

        return StreamingResponse(
            chunks(),
            media_type="application/x-ndjson" if format == "jsonl" else "text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="futures-archive.{format}"',
                "Cache-Control": "no-store",
            },
        )
