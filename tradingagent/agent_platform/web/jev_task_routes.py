"""Single workbench API; every action resolves an explicit persisted task identity."""

from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from pydantic import Field
from starlette.responses import StreamingResponse

from agent_platform.domain.futures_chart import KlineInterval
from agent_platform.domain.jev_tasks import JevTaskCreate
from agent_platform.ports.futures_market import FuturesMarketUnavailable
from agent_platform.ports.session_analysis import HistoricalDataUnavailable
from agent_platform.ports.sessions import PersistenceUnavailable
from agent_platform.web.feedback_routes import Confirmation
from agent_platform.web.futures_trading_routes import action_failure_code


class JevTaskAction(Confirmation):
    expected_revision: int = Field(strict=True, ge=0)
    session_revision: int = Field(strict=True, ge=1)


class JevCadenceAction(JevTaskAction):
    cadence_revision: int = Field(strict=True, ge=0)
    decision_interval_seconds: int = Field(strict=True, ge=1, le=10)


class JevInputAction(JevTaskAction):
    context_revision: int = Field(strict=True, ge=0)
    context_mode: Literal["legacy", "multiscale"]


def register_jev_task_routes(app, guard):
    def manager(request):
        value = getattr(request.app.state, "jev_tasks", None)
        if value is None:
            raise HTTPException(503, "JEV 多会话工作台尚未启用。")
        return value

    async def context(request, task_id):
        try:
            return await manager(request).context(task_id)
        except LookupError:
            raise HTTPException(404, "会话不存在，请重新载入会话列表。") from None

    @app.get("/api/jev-tasks", dependencies=[Depends(guard.require_read)])
    async def listing(request: Request):
        return await manager(request).list()

    @app.post("/api/jev-tasks", status_code=201, dependencies=[Depends(guard.require_write)])
    async def create(request: Request, selection: JevTaskCreate):
        value = manager(request)
        try:
            await value.base.initial_analysis.validate_target(selection.analysis_target)
            return await value.create(selection)
        except HistoricalDataUnavailable:
            raise HTTPException(503, "合约目录暂不可读，请检查公共行情连接后重试。") from None
        except ValueError:
            raise HTTPException(409, "合约或会话配置不可用，请重新载入后核对。") from None

    @app.get("/api/jev-tasks/{task_id}", dependencies=[Depends(guard.require_read)])
    async def view(request: Request, task_id: str):
        await context(request, task_id)
        return await manager(request).view(task_id)

    @app.put("/api/jev-tasks/{task_id}/cadence", dependencies=[Depends(guard.require_write)])
    async def cadence(request: Request, task_id: str, selection: JevCadenceAction):
        await context(request, task_id)
        try:
            return await manager(request).update_cadence(task_id, selection)
        except ValueError as error:
            message = (
                "请先暂停此会话，再调整分析间隔。"
                if str(error) == "cadence_requires_paused"
                else "会话或分析间隔版本已变化，请刷新后重试。"
            )
            raise HTTPException(409, message) from None

    @app.put("/api/jev-tasks/{task_id}/context", dependencies=[Depends(guard.require_write)])
    async def input_mode(request: Request, task_id: str, selection: JevInputAction):
        await context(request, task_id)
        try:
            return await manager(request).update_context(task_id, selection)
        except ValueError as error:
            message = (
                "请先暂停此会话，再切换 JEV 输入方式。"
                if str(error) == "context_requires_paused"
                else "输入方式未切换；请刷新并检查背景模型配置及会话版本。"
            )
            raise HTTPException(409, message) from None

    @app.post("/api/jev-tasks/{task_id}/{operation}", dependencies=[Depends(guard.require_write)])
    async def action(
        request: Request,
        task_id: str,
        operation: Literal["start", "pause", "close"],
        selection: JevTaskAction,
    ):
        await context(request, task_id)
        try:
            value = manager(request)
            if operation == "start":
                return await value.start(
                    task_id, selection.session_revision, selection.expected_revision
                )
            return await value.pause(
                task_id,
                selection.session_revision,
                selection.expected_revision,
                close=operation == "close",
            )
        except (ValueError, FuturesMarketUnavailable, TimeoutError) as error:
            reason = (
                "close_requires_flat_position"
                if str(error) == "close_requires_flat_position"
                else action_failure_code(error)
            )
            raise HTTPException(409, "操作未完成，请核对当前状态后重试。原因：" + reason) from None

    @app.get("/api/jev-tasks/{task_id}/history", dependencies=[Depends(guard.require_read)])
    async def history(
        request: Request,
        task_id: str,
        interval: KlineInterval = "1h",
        limit: int = Query(200, ge=1, le=499),
    ):
        _, services, session = await context(request, task_id)
        active = await services.sessions.store.active()
        if session is None or active is None or active.session_id != session.session_id:
            raise HTTPException(409, "已结束会话的历史存档请从交易档案查看。")
        try:
            return await services.initial_analysis.chart_history(interval, limit)
        except (HistoricalDataUnavailable, TimeoutError):
            raise HTTPException(503, "所选合约历史暂不可读，稍后重试。") from None

    async def archive_context(request, task_id):
        _, services, session = await context(request, task_id)
        if session is None:
            raise HTTPException(409, "会话尚未初始化。")
        svc = services.futures_trading
        if await svc.store.run(svc.scope(session).account_ref) is None:
            raise HTTPException(409, "本会话尚未建立虚拟钱包，没有交易档案。")
        return svc.archive, svc.scope(session).account_ref

    @app.get("/api/jev-tasks/{task_id}/archive", dependencies=[Depends(guard.require_read)])
    async def archive(
        request: Request,
        task_id: str,
        after: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        through: int | None = Query(None, ge=0),
    ):
        store, account_ref = await archive_context(request, task_id)
        try:
            return (
                await store.page(account_ref, after=after, limit=limit, through=through)
            ).model_dump(mode="json")
        except (ValueError, LookupError, PersistenceUnavailable):
            raise HTTPException(409, "档案不可读或完整性校验失败，请保留数据库并核对。") from None

    @app.get("/api/jev-tasks/{task_id}/archive/export", dependencies=[Depends(guard.require_read)])
    async def export(request: Request, task_id: str, format: Literal["jsonl", "csv"] = "jsonl"):
        store, account_ref = await archive_context(request, task_id)
        try:
            output = await store.export(account_ref, format=format)
        except (ValueError, LookupError, PersistenceUnavailable):
            raise HTTPException(409, "档案导出未完成，完整性校验失败或存储不可用。") from None

        def chunks():
            try:
                while data := output.read(65536):
                    yield data
            finally:
                output.close()

        return StreamingResponse(
            chunks(),
            media_type="application/x-ndjson" if format == "jsonl" else "text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="jev-{task_id}.{format}"'},
        )
