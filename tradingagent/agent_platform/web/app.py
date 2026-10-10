"""Loopback-only session settings and read-only latest-frame overview."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Self

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from agent_platform.application.agent_controls import ControlEnvironmentConflict
from agent_platform.bootstrap import ApplicationServices, build_application_services
from agent_platform.config import RuntimeConfig
from agent_platform.domain.agent_controls import AdviceSelection, ControlSelection, TraderSelection
from agent_platform.domain.models import Revision, StyleStrength
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.sessions import AgentSession, TradingStyle
from agent_platform.ports.persistence import EventIdentityConflict, RequestIdentityConflict
from agent_platform.ports.session_analysis import HistoricalDataUnavailable
from agent_platform.ports.sessions import (
    ActiveSessionExists,
    PersistenceUnavailable,
    RevisionConflict,
    SessionNotFound,
)
from agent_platform.web.event_agent_routes import register_event_agent_routes
from agent_platform.web.feedback_routes import register_feedback_routes
from agent_platform.web.futures_trading_routes import register_futures_trading_routes
from agent_platform.web.jev_task_routes import register_jev_task_routes
from agent_platform.web.paper_routes import register_paper_routes
from agent_platform.web.review_routes import register_review_routes
from agent_platform.web.security import LocalBrowserSession
from agent_platform.web.session_analysis_routes import register_session_analysis_routes

WEB_ROOT = Path(__file__).resolve().parent


class StyleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    style_strength: StyleStrength
    style_confirmed: StrictBool

    @model_validator(mode="after")
    def explicitly_confirmed(self) -> Self:
        if self.style_confirmed is not True:
            raise ValueError("style selection requires explicit confirmation")
        return self


class StyleUpdate(StyleSelection):
    expected_revision: Revision


class SessionCreate(StyleSelection):
    analysis_target: SessionAnalysisTarget = SessionAnalysisTarget()


class ControlUpdate(ControlSelection):
    expected_revision: int = Field(strict=True, ge=0)


class AdviceUpdate(AdviceSelection):
    expected_revision: int = Field(strict=True, ge=0)


class TraderUpdate(TraderSelection):
    expected_revision: int = Field(strict=True, ge=0)


class SessionStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    status: Literal["running", "paused", "closed"]
    expected_revision: Revision


def create_app(
    database_path: Path,
    *,
    services: ApplicationServices | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> FastAPI:
    if services is not None and runtime_config is not None:
        raise ValueError("provide either injected services or a runtime configuration")
    guard = LocalBrowserSession()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is not None:
            app.state.services = services
            app.state.sessions = services.sessions
            try:
                await services.runtime.start()
                yield
            finally:
                await services.runtime.stop()
        else:
            async with build_application_services(database_path, runtime_config) as assembled:
                app.state.services = assembled
                app.state.sessions = assembled.sessions
                if runtime_config is not None and runtime_config.jev_workbench:
                    from agent_platform.application.jev_tasks import JevTaskWorkbench

                    manager = JevTaskWorkbench(database_path, runtime_config, assembled)
                    app.state.jev_tasks = await manager.open()
                    try:
                        yield
                    finally:
                        await manager.shutdown()
                else:
                    yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_ROOT / "templates")

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_selection(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "请输入0–100的整数风格并明确确认；版本信息或其他输入有误时请刷新。"
                if request.url.path.startswith("/api/sessions")
                else "输入格式有误；请明确确认，并核对金额、时间、标识与整数版本。",
            },
        )

    @app.exception_handler(ActiveSessionExists)
    @app.exception_handler(RevisionConflict)
    async def conflict(request: Request, error: Exception):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "会话已存在或已被修改，请重新载入当前会话后再保存。",
            },
        )

    @app.exception_handler(SessionNotFound)
    async def not_found(request: Request, error: Exception):
        return JSONResponse(status_code=404, content={"detail": "会话不存在，请重新载入。"})

    @app.exception_handler(PersistenceUnavailable)
    async def unavailable(request: Request, error: Exception):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "本地存储暂不可用，设置未确认保存。请恢复存储后重新载入。",
            },
        )

    @app.exception_handler(ControlEnvironmentConflict)
    async def control_environment_conflict(request: Request, error: Exception):
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "已保存的模式与当前生产数据环境冲突。"
                    "请在离线实例恢复建议模式，或关闭生产读取后重启。"
                )
            },
        )

    @app.exception_handler(RequestIdentityConflict)
    @app.exception_handler(EventIdentityConflict)
    async def record_conflict(request: Request, error: Exception):
        return JSONResponse(
            status_code=409,
            content={"detail": "记录标识或审计事实存在冲突，请重新载入并核对操作。"},
        )

    register_feedback_routes(app, guard)
    register_review_routes(app, guard)
    register_paper_routes(app, guard)
    register_futures_trading_routes(app, guard)
    register_session_analysis_routes(app, guard)
    register_jev_task_routes(app, guard)
    register_event_agent_routes(app, guard)

    @app.get("/")
    @app.get("/overview")
    @app.get("/records")
    @app.get("/reviews")
    @app.get("/status")
    @app.get("/agent")
    @app.get("/jev-trader")
    @app.get("/workbench")
    @app.get("/event-agent")
    async def index(request: Request):
        cookie = request.cookies.get(guard.cookie_name)
        if not guard.valid(cookie):
            cookie = guard.issue()
        response = templates.TemplateResponse(
            request=request,
            name="jev-workspace.html"
            if getattr(app.state, "jev_tasks", None)
            and request.url.path in ("/", "/workbench", "/jev-trader")
            else {
                "/": "session.html",
                "/overview": "overview.html",
                "/records": "records.html",
                "/reviews": "reviews.html",
                "/status": "status.html",
                "/agent": "overview.html",
                "/jev-trader": "jev-trader.html",
                "/workbench": "jev-trader.html",
                "/event-agent": "event-agent.html",
            }[request.url.path],
            context={"csrf_token": guard.csrf(cookie)},
        )
        response.set_cookie(
            guard.cookie_name,
            cookie,
            max_age=guard.max_age,
            httponly=True,
            samesite="strict",
            secure=False,
        )
        return response

    async def view(session: AgentSession | None) -> dict:
        history = await app.state.sessions.store.history(session.session_id) if session else ()
        overview = await app.state.services.queries.overview()
        return {
            "session": session.model_dump(mode="json") if session else None,
            "style_label": session.style.label if session else None,
            "style_context": session.style.context.model_dump(mode="json") if session else None,
            "style_history": [
                {
                    "revision": event.session.style_revision,
                    "strength": event.session.style.strength,
                    "changed_at": event.occurred_at.isoformat(),
                }
                for event in history
                if event.kind in ("created", "style_changed")
                and event.session.revision <= session.revision
            ],
            "readiness": {"market": overview.market.status, "account": overview.account.status},
        }

    @app.get("/api/session", dependencies=[Depends(guard.require_read)])
    async def current_session():
        return await view(await app.state.sessions.store.active())

    def control_service():
        if app.state.services.controls is None:
            raise HTTPException(status_code=503, detail="模式设置尚未装配，请重新启动本地服务。")
        return app.state.services.controls

    @app.get("/api/controls", dependencies=[Depends(guard.require_read)])
    async def current_controls():
        return await control_service().public_view()

    @app.post("/api/controls", dependencies=[Depends(guard.require_write)])
    async def update_controls(selection: ControlUpdate):
        service = control_service()
        try:
            await service.update(
                ControlSelection(
                    mode=selection.mode,
                    execution_environment=selection.execution_environment,
                    jev_enabled=selection.jev_enabled,
                    trader_enabled=selection.trader_enabled,
                    confirmed=selection.confirmed,
                ),
                expected_revision=selection.expected_revision,
            )
        except ControlEnvironmentConflict:
            raise
        except RevisionConflict:
            raise HTTPException(
                status_code=409, detail="模式设置已被修改，请重新载入后保存。"
            ) from None
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="自动模式仅用于 testnet；请先关闭生产账户与行情读取并重启服务。",
            ) from None
        return await service.public_view()

    @app.post("/api/controls/advice", dependencies=[Depends(guard.require_write)])
    async def update_advice(selection: AdviceUpdate):
        service = control_service()
        await service.update_advice(
            AdviceSelection(enabled=selection.enabled, confirmed=selection.confirmed),
            expected_revision=selection.expected_revision,
        )
        return await service.public_view()

    @app.post("/api/controls/trader", dependencies=[Depends(guard.require_write)])
    async def update_trader(selection: TraderUpdate):
        service = control_service()
        try:
            await service.update_trader(
                TraderSelection(
                    mode=selection.mode,
                    execution_environment=selection.execution_environment,
                    enabled=selection.enabled,
                    confirmed=selection.confirmed,
                ),
                expected_revision=selection.expected_revision,
            )
        except ControlEnvironmentConflict:
            raise
        except RevisionConflict:
            raise
        except ValueError:
            raise HTTPException(
                status_code=422, detail="操盘模块仅用于 testnet，当前生产数据源不能用于自动执行。"
            ) from None
        return await service.public_view()

    @app.get("/api/overview", dependencies=[Depends(guard.require_read)])
    async def overview():
        return await app.state.services.queries.overview()

    @app.get("/api/events", dependencies=[Depends(guard.require_read)])
    async def events(request: Request):
        async def frames():
            async for frame in app.state.services.queries.watch(
                request.headers.get("Last-Event-ID")
            ):
                if await request.is_disconnected() or not guard.valid(
                    request.cookies.get(guard.cookie_name)
                ):
                    return
                if frame is None:
                    yield ": keepalive\n\n"
                else:
                    yield (
                        f"id: {frame.event_id}\nevent: overview\n"
                        f"data: {frame.model_dump_json()}\n\n"
                    )

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions", status_code=201, dependencies=[Depends(guard.require_write)])
    async def create_session(selection: SessionCreate):
        analysis = app.state.services.initial_analysis
        if not selection.analysis_target.legacy_spot:
            if analysis is None:
                raise HTTPException(
                    status_code=503, detail="合约行情尚未装配，请启用公共行情后重启。"
                )
            try:
                await analysis.validate_target(selection.analysis_target)
            except HistoricalDataUnavailable:
                raise HTTPException(
                    status_code=503, detail="合约目录暂不可读，请核对公共行情连接后重试。"
                ) from None
            except ValueError:
                raise HTTPException(
                    status_code=422, detail="所选合约不在当前可交易的 USDT 永续目录中。"
                ) from None
        session = await app.state.sessions.create(
            TradingStyle(strength=selection.style_strength), selection.analysis_target
        )
        return await view(session)

    @app.put("/api/sessions/{session_id}/style", dependencies=[Depends(guard.require_write)])
    async def change_style(session_id: str, selection: StyleUpdate):
        session = await app.state.sessions.change_style(
            session_id,
            TradingStyle(strength=selection.style_strength),
            selection.expected_revision,
        )
        return await view(session)

    @app.put("/api/sessions/{session_id}/state", dependencies=[Depends(guard.require_write)])
    async def change_state(session_id: str, selection: SessionStateUpdate):
        try:
            session = await app.state.sessions.transition(
                session_id,
                selection.status,
                selection.expected_revision,
            )
        except RevisionConflict:
            raise
        except ValueError:
            raise HTTPException(
                status_code=422, detail="当前会话不允许此状态操作，请重新载入。"
            ) from None
        return await view(session)

    return app
