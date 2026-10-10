"""Local browser controls. No endpoint accepts a model fee grant."""

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool

from agent_platform.domain.futures_values import Amount, Quantity
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.domain.watches import WatchDefinition
from agent_platform.ports.sessions import RevisionConflict


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(strict=True, ge=1)
    confirmed: StrictBool


class Configure(Operation):
    policy: TradingPolicy
    limits: TradingLimits
    qty_step: Quantity
    min_qty: Quantity
    max_qty: Quantity
    min_notional: Amount


class Analyze(Operation):
    request_id: str = Field(min_length=1, max_length=128)


class WatchWrite(Operation):
    definition: dict[str, JsonValue]


class WatchCancel(Operation):
    lane_revision: int = Field(strict=True, ge=1)


def register_event_agent_routes(app, guard):
    def service():
        value = getattr(app.state.services, "event_agent", None)
        if value is None:
            raise HTTPException(503, "事件 Agent 尚未装配；请启用独立离线配置。")
        return value

    def confirmed(selection):
        if selection.confirmed is not True:
            raise HTTPException(422, "请明确确认此操作。")

    @app.get("/api/event-agent/status", dependencies=[Depends(guard.require_read)])
    async def status():
        value = getattr(app.state.services, "event_agent", None)
        return (
            await value.status()
            if value
            else {
                "available": False,
                "execution_environment": "paper",
                "paid_calls_enabled": False,
                "protection_status": "not_configured",
            }
        )

    @app.post("/api/event-agent/configure", dependencies=[Depends(guard.require_write)])
    async def configure(selection: Configure):
        confirmed(selection)
        try:
            return (
                await service().configure(
                    selection.model_dump(exclude={"expected_revision", "confirmed"}),
                    selection.expected_revision,
                )
            ).model_dump(mode="json")
        except RevisionConflict:
            raise
        except ValueError as error:
            raise HTTPException(422, str(error)[:256]) from None

    async def activation(selection, enabled):
        confirmed(selection)
        return (await service().set_enabled(enabled, selection.expected_revision)).model_dump(
            mode="json"
        )

    @app.post("/api/event-agent/start", dependencies=[Depends(guard.require_write)])
    async def start(selection: Operation):
        return await activation(selection, True)

    @app.post("/api/event-agent/pause", dependencies=[Depends(guard.require_write)])
    async def pause(selection: Operation):
        return await activation(selection, False)

    @app.post("/api/event-agent/analyze", dependencies=[Depends(guard.require_write)])
    async def analyze(selection: Analyze):
        confirmed(selection)
        try:
            return (
                await service().analyze(selection.expected_revision, selection.request_id)
            ).model_dump(mode="json")
        except RevisionConflict:
            raise
        except ValueError as error:
            raise HTTPException(409, str(error)[:256]) from None

    @app.get("/api/event-agent/watches", dependencies=[Depends(guard.require_read)])
    async def watches():
        return [
            w.model_dump(mode="json") for w in await service().watches.list_all(service().lane_id)
        ]

    @app.post("/api/event-agent/watches", dependencies=[Depends(guard.require_write)])
    async def create(selection: WatchWrite):
        confirmed(selection)
        try:
            return (
                await service().create_watch(selection.definition, selection.expected_revision)
            ).model_dump(mode="json")
        except RevisionConflict:
            raise
        except ValueError:
            raise HTTPException(422, "Watch 参数或账户归属不合法。") from None

    @app.put("/api/event-agent/watches/{watch_id}", dependencies=[Depends(guard.require_write)])
    async def replace(watch_id: str, selection: WatchWrite):
        confirmed(selection)
        import json

        svc = service()
        old = await svc.watches.get(watch_id)
        value = WatchDefinition.model_validate_json(json.dumps(selection.definition))
        lane = await svc.runs.lane(svc.lane_id)
        if (
            old.definition.lane_id,
            value.lane_id,
            value.session_id,
            value.symbol,
            value.watch_id,
        ) != (lane.lane_id, lane.lane_id, lane.session_id, lane.symbol, watch_id):
            raise HTTPException(422, "Watch 账户归属不合法。")
        return (await svc.watches.replace(value, selection.expected_revision)).model_dump(
            mode="json"
        )

    @app.post(
        "/api/event-agent/watches/{watch_id}/cancel", dependencies=[Depends(guard.require_write)]
    )
    async def cancel(watch_id: str, selection: WatchCancel):
        confirmed(selection)
        svc = service()
        lane = await svc.runs.lane(svc.lane_id)
        if lane.revision != selection.lane_revision:
            raise RevisionConflict("lane_changed")
        old = await svc.watches.get(watch_id)
        if old.definition.lane_id != lane.lane_id:
            raise HTTPException(422, "Watch 账户归属不合法。")
        return (
            await svc.watches.cancel(watch_id, selection.expected_revision, svc.clock.utcnow())
        ).model_dump(mode="json")
