"""Mode-specific tools. Ownership and identities come from the runtime, never the model."""

from datetime import datetime
from hashlib import sha256

from agent_platform.domain.agent_tools import ToolResult, ToolSpec
from agent_platform.domain.watches import WatchDefinition
from agent_platform.ports.sessions import RevisionConflict

_FIELDS = {
    "get_market_snapshot": {},
    "get_indicator_catalog": {},
    "list_watches": {},
    "get_account_state": {},
    "get_watch_event": {},
    "create_watch": {
        "timeframe": {"enum": ["1m", "5m"]},
        "hypothesis": {"type": "string"},
        "expires_at": {"type": "string"},
        "trigger": {"type": "object"},
        "invalidation": {"type": ["object", "null"]},
    },
    "update_watch": {
        "watch_id": {"type": "string"},
        "expected_revision": {"type": "integer"},
        "timeframe": {"enum": ["1m", "5m"]},
        "hypothesis": {"type": "string"},
        "expires_at": {"type": "string"},
        "trigger": {"type": "object"},
        "invalidation": {"type": ["object", "null"]},
    },
    "cancel_watch": {"watch_id": {"type": "string"}, "expected_revision": {"type": "integer"}},
    "preview_trade": {
        "action": {"enum": ["open_long", "open_short", "reduce"]},
        "quantity": {"type": "string"},
        "target_leverage": {"type": ["integer", "null"]},
        "protective_stop_mark": {"type": ["string", "null"]},
    },
}
_FIELDS["submit_trade_intent"] = _FIELDS["preview_trade"]
_REQUIRED = {
    "create_watch": ("timeframe", "hypothesis", "expires_at", "trigger"),
    "update_watch": ("watch_id", "expected_revision"),
    "cancel_watch": ("watch_id", "expected_revision"),
    "preview_trade": ("action", "quantity"),
    "submit_trade_intent": ("action", "quantity"),
}


def valid_arguments(name, args):
    if any(key not in args for key in _REQUIRED.get(name, ())):
        return False
    types = {"string": str, "integer": int, "object": dict, "null": type(None)}
    for key, value in args.items():
        field = _FIELDS[name][key]
        expected = field.get("type")
        if expected is not None:
            allowed = expected if isinstance(expected, list) else [expected]
            if type(value) not in [types[t] for t in allowed]:
                return False
        if "enum" in field and value not in field["enum"]:
            return False
    return True


_ANALYSIS = (
    "get_market_snapshot",
    "get_indicator_catalog",
    "create_watch",
    "update_watch",
    "cancel_watch",
    "list_watches",
)
_REVIEW = (
    "get_market_snapshot",
    "get_account_state",
    "get_watch_event",
    "preview_trade",
    "submit_trade_intent",
    "create_watch",
)


def watch_summary(watch):
    d = watch.definition
    return {
        "watch_id": d.watch_id,
        "revision": watch.revision,
        "definition_revision": d.version,
        "state": watch.state,
        "timeframe": d.timeframe,
        "hypothesis": d.hypothesis[:128],
        "rule_hash": d.rule_hash,
        "expires_at": d.expires_at.isoformat(),
    }


class ToolRegistry:
    def __init__(self, *, runs, watches, context, clock, intents=None):
        self.runs, self.watches, self.context, self.clock, self.intents = (
            runs,
            watches,
            context,
            clock,
            intents,
        )

    def available(self, mode):
        names = _ANALYSIS if mode == "ANALYSIS" else _REVIEW if mode == "REVIEW" else ()
        return tuple(
            ToolSpec(
                name=n,
                description=n.replace("_", " "),
                parameters={
                    "type": "object",
                    "properties": _FIELDS[n],
                    "additionalProperties": False,
                    "required": list(_REQUIRED.get(n, ())),
                },
            )
            for n in names
        )

    async def execute(self, call, run, lane):
        def result(status, data=None, reason=None):
            return ToolResult(
                tool_call_id=call.tool_call_id,
                name=call.name,
                status=status,
                data=data or {},
                reason=reason,
            )

        if call.name not in {t.name for t in self.available(run.mode)}:
            return result("rejected", reason="mode_permission")
        if set(call.arguments) - set(_FIELDS[call.name]):
            return result("rejected", reason="unknown_or_scope_parameter")
        if not valid_arguments(call.name, call.arguments):
            return result("rejected", reason="invalid_tool_arguments")
        current = await self.runs.lane(lane.lane_id)
        persisted = await self.runs.get(run.run_id)
        if (
            current != lane
            or persisted.lane != lane
            or not current.enabled
            or persisted.status != "RUNNING"
            or self.clock.utcnow() >= run.deadline
        ):
            return result("rejected", reason="run_retired")
        args = call.arguments
        try:
            if call.name == "get_market_snapshot":
                ctx = await self.context.for_analysis(lane)
                return result(
                    "ok",
                    {
                        "market": ctx.latest_market.model_dump(mode="json")
                        if ctx.latest_market
                        else None,
                        "unavailable": list(ctx.unavailable),
                    },
                )
            if call.name == "get_indicator_catalog":
                return result(
                    "ok",
                    {
                        "metrics": [
                            "candle.open",
                            "candle.high",
                            "candle.low",
                            "candle.close",
                            "ema_12",
                            "ema_26",
                            "atr_14",
                            "volume_ratio_20",
                        ],
                        "intervals": ["1m", "5m"],
                        "closed_candles_only": True,
                    },
                )
            if call.name == "list_watches":
                return result(
                    "ok",
                    {
                        "watches": [
                            watch_summary(w) for w in await self.watches.list_active(lane.lane_id)
                        ]
                    },
                )
            if call.name == "get_watch_event":
                return result(
                    "ok",
                    {
                        "trigger": run.context.trigger.model_dump(mode="json")
                        if run.context and run.context.trigger
                        else None
                    },
                )
            if call.name == "get_account_state":
                ctx = await self.context.for_analysis(lane)
                return result(
                    "ok" if ctx.account else "unavailable",
                    {"account": ctx.account.model_dump(mode="json") if ctx.account else None},
                )
            if call.name in ("preview_trade", "submit_trade_intent"):
                if self.intents is None:
                    return result("unavailable", reason="execution_not_configured")
                value = await self.intents.execute_tool(call, run, lane)
                return result("ok", value)
            if call.name in ("update_watch", "cancel_watch"):
                old = await self.watches.get(args["watch_id"])
                if (old.definition.lane_id, old.definition.session_id) != (
                    lane.lane_id,
                    lane.session_id,
                ):
                    return result("rejected", reason="watch_scope")
                if call.name == "cancel_watch":
                    value = await self.watches.cancel(
                        args["watch_id"], args["expected_revision"], self.clock.utcnow()
                    )
                    return result("ok", {"watch": watch_summary(value)})
                fields = old.definition.model_dump()
                fields.update(
                    {k: v for k, v in args.items() if k not in ("watch_id", "expected_revision")}
                )
                fields.update(
                    definition_revision=old.definition.version + 1, created_at=self.clock.utcnow()
                )
                fields["expires_at"] = (
                    datetime.fromisoformat(fields["expires_at"])
                    if isinstance(fields["expires_at"], str)
                    else fields["expires_at"]
                )
                value = await self.watches.replace(
                    WatchDefinition.model_validate(fields), args["expected_revision"]
                )
            else:
                key = sha256(f"{run.run_id}:{call.tool_call_id}".encode()).hexdigest()
                value = await self.watches.create(
                    WatchDefinition.model_validate(
                        args
                        | {
                            "expires_at": datetime.fromisoformat(args["expires_at"]),
                            "watch_id": "agent-watch:" + key,
                            "definition_revision": 1,
                            "lane_id": lane.lane_id,
                            "session_id": lane.session_id,
                            "symbol": lane.symbol,
                            "created_at": self.clock.utcnow(),
                        }
                    )
                )
            return result("ok", {"watch": watch_summary(value)})
        except (ValueError, TypeError, KeyError, RevisionConflict) as error:
            return result("rejected", reason=str(error)[:256] or "invalid_tool_arguments")
