"""Protected virtual-wallet controls use the server's active paper account."""

from fastapi import Depends, HTTPException, Request

from agent_platform.domain.models import Identifier, Revision
from agent_platform.domain.paper_trading import PaperSettings
from agent_platform.ports.paper import PaperGuardConflict
from agent_platform.ports.sessions import RevisionConflict
from agent_platform.web.feedback_routes import Confirmation


class PaperConfigure(Confirmation):
    settings: PaperSettings
    session_id: Identifier
    style_revision: Revision


class PaperAction(Confirmation):
    expected_revision: Revision
    account_ref: Identifier
    style_revision: Revision
    activation_revision: Revision


def register_paper_routes(app, guard):
    def service(request):
        value = request.app.state.services.paper
        if value is None:
            raise HTTPException(
                status_code=503, detail="Paper 未装配，请使用 --paper 并明确选择决策来源后启动。"
            )
        return value

    @app.get("/api/paper", dependencies=[Depends(guard.require_read)])
    async def current(request: Request):
        value = request.app.state.services.paper
        if value is None:
            session = await request.app.state.services.sessions.store.active()
            return {
                "enabled": False,
                "environment": "paper",
                "real_orders_enabled": False,
                "paid_models_enabled": False,
                "account": None,
                "cycles": [],
                "paper_market": "spot",
                "analysis_target": session.analysis_target.model_dump(mode="json")
                if session
                else None,
                "market_compatible": session is None or session.analysis_target.legacy_spot,
                "session": {
                    "session_id": session.session_id,
                    "style_revision": session.style_revision,
                    "style_strength": session.style.strength,
                }
                if session
                else None,
            }
        return await value.public_view()

    @app.get("/api/paper/cycles", dependencies=[Depends(guard.require_read)])
    async def cycles(request: Request):
        return (await current(request))["cycles"]

    @app.post("/api/paper/configure", dependencies=[Depends(guard.require_write)])
    async def configure(request: Request, selection: PaperConfigure):
        value = service(request)
        try:
            await value.configure(
                selection.settings,
                session_id=selection.session_id,
                style_revision=selection.style_revision,
            )
        except PaperGuardConflict:
            raise HTTPException(
                status_code=409, detail="先确认会话风格，再保存开启操盘、自动方式与 Paper 环境。"
            ) from None
        return await value.public_view()

    async def action(request, selection, name):
        value = service(request)
        account = await value.store.latest()
        if account is None:
            raise HTTPException(status_code=409, detail="尚未配置当前会话的虚拟资金账户。")
        if account.account_ref != selection.account_ref:
            raise HTTPException(
                status_code=409, detail="模拟账户已变化；旧页面确认不能用于新会话。"
            )
        try:
            if name == "start":
                await value.start(
                    account.account_ref,
                    selection.expected_revision,
                    style_revision=selection.style_revision,
                )
            else:
                await value.pause(
                    account.account_ref,
                    selection.expected_revision,
                    activation_revision=selection.activation_revision,
                )
        except PaperGuardConflict:
            raise HTTPException(
                status_code=409, detail="会话、操盘配置或数据来源已变化；请重新载入并核对。"
            ) from None
        except RevisionConflict:
            raise
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Paper 模型试跑配置已失效，请核对本机费用与有效期。"
            ) from None
        return await value.public_view()

    @app.post("/api/paper/start", dependencies=[Depends(guard.require_write)])
    async def start(request: Request, selection: PaperAction):
        return await action(request, selection, "start")

    @app.post("/api/paper/pause", dependencies=[Depends(guard.require_write)])
    async def pause(request: Request, selection: PaperAction):
        return await action(request, selection, "pause")
