"""Lane compare-and-swap and append-before-dispatch run history."""

from contextlib import closing
from datetime import timedelta
from hashlib import sha256

from agent_platform.domain.common import utc_datetime
from agent_platform.domain.event_agent import (
    AgentContext,
    AgentFinalDecision,
    AgentRun,
    AgentToolRecord,
    AgentTurnRecord,
    AgentTurnRequest,
    AgentTurnResponse,
    EventAgentLane,
)
from agent_platform.ports.event_agent import RunAlreadyStarted
from agent_platform.ports.sessions import RevisionConflict

from .events import SqliteStore


def load_run(db, run_id):
    row = db.execute("SELECT body FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("agent run not found")
    return AgentRun.model_validate_json(row[0])


def load_lane(db, lane_id):
    row = db.execute("SELECT body FROM event_agent_lanes WHERE lane_id=?", (lane_id,)).fetchone()
    if row is None:
        raise ValueError("agent lane not found")
    return EventAgentLane.model_validate_json(row[0])


def update_run(db, run, **changes):
    value = AgentRun.model_validate(run.model_dump() | changes | {"revision": run.revision + 1})
    db.execute(
        "UPDATE agent_runs SET status=?,body=? WHERE run_id=?",
        (value.status, value.model_dump_json(), value.run_id),
    )
    return value


def active_run(db, run):
    if (
        run.status != "RUNNING"
        or load_lane(db, run.lane.lane_id) != run.lane
        or not run.lane.enabled
    ):
        raise ValueError("agent run is retired")


class SqliteAgentRunStore(SqliteStore):
    async def _transaction(self, operation):
        def write():
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                return operation(db)

        return await self._io(write)

    async def save_lane(self, lane, expected_revision):
        lane = EventAgentLane.model_validate_json(lane.model_dump_json())

        def write(db):
            row = db.execute(
                "SELECT body FROM event_agent_lanes WHERE lane_id=?", (lane.lane_id,)
            ).fetchone()
            if row is None:
                if expected_revision != 0 or lane.revision != 1:
                    raise RevisionConflict("new lane requires revision zero")
                if db.execute(
                    "SELECT 1 FROM event_agent_lanes WHERE account_ref=?", (lane.scope.account_ref,)
                ).fetchone():
                    raise ValueError("Paper wallet already belongs to another lane")
                db.execute(
                    "INSERT INTO event_agent_lanes VALUES(?,?,?,?)",
                    (lane.lane_id, lane.scope.account_ref, 1, lane.model_dump_json()),
                )
            else:
                old = EventAgentLane.model_validate_json(row[0])
                if type(expected_revision) is not int or old.revision != expected_revision:
                    raise RevisionConflict("agent lane revision changed")
                if lane.scope != old.scope or lane.session_id != old.session_id:
                    raise ValueError("agent lane ownership cannot change")
                # Wallet arithmetic settings are fixed once funds are initialized.
                fields = ("limits", "qty_step", "min_qty", "max_qty", "min_notional")
                if db.execute(
                    "SELECT 1 FROM event_paper_wallets WHERE account_ref=?",
                    (old.scope.account_ref,),
                ).fetchone() and any(getattr(old, k) != getattr(lane, k) for k in fields):
                    raise ValueError("initialized wallet risk settings are fixed")
                if lane.revision != old.revision + 1:
                    raise RevisionConflict("agent lane must use the next revision")
                db.execute(
                    "UPDATE event_agent_lanes SET revision=?,body=? WHERE lane_id=?",
                    (lane.revision, lane.model_dump_json(), lane.lane_id),
                )
            return lane

        return await self._transaction(write)

    async def lane(self, lane_id):
        def read():
            with closing(self._connect()) as db:
                return load_lane(db, lane_id)

        return await self._io(read)

    async def lanes(self):
        def read():
            with closing(self._connect()) as db:
                return tuple(
                    EventAgentLane.model_validate_json(row[0])
                    for row in db.execute("SELECT body FROM event_agent_lanes ORDER BY lane_id")
                )

        return await self._io(read)

    async def recent(self, lane_id, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("run page must be bounded")

        def read():
            with closing(self._connect()) as db:
                return tuple(
                    AgentRun.model_validate_json(row[0])
                    for row in db.execute(
                        "SELECT body FROM agent_runs WHERE lane_id=? ORDER BY rowid DESC LIMIT ?",
                        (lane_id, limit),
                    )
                )

        return await self._io(read)

    async def _claim(self, key, lane, at, event=None, executor_id=None):
        at = utc_datetime(at)
        lane = EventAgentLane.model_validate_json(lane.model_dump_json())

        def write(db):
            row = db.execute("SELECT body FROM agent_runs WHERE request_key=?", (key,)).fetchone()
            if row is not None:
                prior = AgentRun.model_validate_json(row[0])
                if prior.lane.lane_id != lane.lane_id or prior.event != event:
                    raise ValueError("agent request identity conflict")
                return prior
            if not lane.enabled or load_lane(db, lane.lane_id) != lane:
                raise ValueError("agent lane is paused or changed")
            if event is not None and (
                (event.lane_id, event.session_id, event.frame.symbol)
                != (lane.lane_id, lane.session_id, lane.symbol)
                or not event.occurred_at <= at < event.expires_at
            ):
                raise ValueError("event scope or TTL no longer authorizes review")
            if db.execute(
                "SELECT 1 FROM agent_runs WHERE lane_id=? AND status IN ('RUNNING','RECONCILING')",
                (lane.lane_id,),
            ).fetchone():
                raise ValueError("agent lane already has an in-flight run")
            run = AgentRun(
                run_id="agent-run:" + sha256(key.encode()).hexdigest(),
                request_key=key,
                executor_id=executor_id,
                lane=lane,
                mode="REVIEW" if event else "ANALYSIS",
                event=event,
                started_at=at,
                deadline=min(
                    at + timedelta(seconds=90),
                    event.expires_at if event else at + timedelta(seconds=90),
                ),
            )
            db.execute(
                "INSERT INTO agent_runs VALUES(?,?,?,?,?)",
                (run.run_id, key, lane.lane_id, run.status, run.model_dump_json()),
            )
            return run

        return await self._transaction(write)

    async def claim(self, event, lane, at, *, executor_id=None):
        return await self._claim("event:" + event.event_id, lane, at, event, executor_id)

    async def claim_analysis(self, lane, request_id, at, *, executor_id=None):
        if not request_id or len(request_id) > 128:
            raise ValueError("analysis request identity must be bounded")
        return await self._claim(
            f"analysis:{lane.lane_id}:{request_id}", lane, at, executor_id=executor_id
        )

    async def get(self, run_id):
        def read():
            with closing(self._connect()) as db:
                return load_run(db, run_id)

        return await self._io(read)

    async def set_context(self, run_id, context):
        context = AgentContext.model_validate_json(context.model_dump_json())

        def write(db):
            run = load_run(db, run_id)
            active_run(db, run)
            if context.lane_id != run.lane.lane_id or context.scope != run.lane.scope:
                raise ValueError("agent context crossed lane scope")
            if run.context is not None:
                raise RunAlreadyStarted("run context already claimed")
            return update_run(db, run, context=context)

        return await self._transaction(write)

    async def record_turn(self, run_id, request, response, status):
        request = AgentTurnRequest.model_validate_json(request.model_dump_json())
        response = (
            AgentTurnResponse.model_validate_json(response.model_dump_json()) if response else None
        )
        if status not in ("SENT", "RESPONDED", "RECONCILING") or (status == "RESPONDED") != (
            response is not None
        ):
            raise ValueError("invalid persisted turn state")

        def write(db):
            run = load_run(db, run_id)
            if request.run_id != run_id or request.lane_id != run.lane.lane_id:
                raise ValueError("turn belongs to another run")
            row = db.execute(
                "SELECT run_id,body FROM agent_turns WHERE request_id=?", (request.request_id,)
            ).fetchone()
            record = AgentTurnRecord(request=request, response=response, status=status)
            turns = list(run.turns)
            if row is None:
                active_run(db, run)
                if status != "SENT" or len(turns) >= 4 or request.deadline > run.deadline:
                    raise ValueError("new turn is not authorized")
                db.execute(
                    "INSERT INTO agent_turns VALUES(?,?,?,?)",
                    (request.request_id, run_id, status, record.model_dump_json()),
                )
                turns.append(record)
            else:
                old = AgentTurnRecord.model_validate_json(row["body"])
                if row["run_id"] != run_id or old.request != request or status == "SENT":
                    raise ValueError("request already sent or has different inputs")
                if old.response is not None:
                    if old != record:
                        raise ValueError("provider result is immutable")
                    return run
                if response is not None and (
                    response.request_id != request.request_id
                    or response.usage.route_id != request.route.route_id
                    or response.usage.model_version != request.route.model_version
                ):
                    raise ValueError("provider result belongs to another route")
                db.execute(
                    "UPDATE agent_turns SET status=?,body=? WHERE request_id=?",
                    (status, record.model_dump_json(), request.request_id),
                )
                turns = [record if t.request.request_id == request.request_id else t for t in turns]
            return update_run(db, run, turns=tuple(turns))

        return await self._transaction(write)

    async def begin_tool(self, run_id, call):
        def write(db):
            run = load_run(db, run_id)
            active_run(db, run)
            prior = next((t for t in run.tools if t.call.tool_call_id == call.tool_call_id), None)
            if prior is not None:
                if prior.call != call:
                    raise ValueError("tool identity conflict")
                raise ValueError("tool already started")
            if len(run.tools) >= 8 or not any(
                t.response and call in t.response.tool_calls for t in run.turns
            ):
                raise ValueError("tool was not requested by an archived response")
            record = AgentToolRecord(call=call)
            db.execute(
                "INSERT INTO agent_tool_calls VALUES(?,?,?)",
                (run_id, call.tool_call_id, record.model_dump_json()),
            )
            return update_run(db, run, tools=(*run.tools, record))

        return await self._transaction(write)

    async def record_tool(self, run_id, call, result):
        def write(db):
            run = load_run(db, run_id)
            prior = next((t for t in run.tools if t.call == call), None)
            if prior is None or (result.tool_call_id, result.name) != (
                call.tool_call_id,
                call.name,
            ):
                raise ValueError("tool result does not match started call")
            if prior.result is not None and prior.result != result:
                raise ValueError("tool result is immutable")
            record = AgentToolRecord(call=call, result=result)
            db.execute(
                "UPDATE agent_tool_calls SET body=? WHERE run_id=? AND tool_call_id=?",
                (record.model_dump_json(), run_id, call.tool_call_id),
            )
            return update_run(
                db, run, tools=tuple(record if t.call == call else t for t in run.tools)
            )

        return await self._transaction(write)

    async def finish(self, run_id, decision, at):
        decision = AgentFinalDecision.model_validate_json(decision.model_dump_json())
        at = utc_datetime(at)

        def write(db):
            run = load_run(db, run_id)
            active_run(db, run)
            if at >= run.deadline:
                raise ValueError("agent run deadline elapsed")
            return update_run(
                db,
                run,
                decision=decision,
                completed_at=at,
                status="WAIT" if decision.action == "WAIT" else "COMPLETED",
            )

        return await self._transaction(write)

    async def abort(self, run_id, status, reason, at):
        if status not in ("FAILED", "EXPIRED", "INTERRUPTED", "RECONCILING"):
            raise ValueError("invalid agent abort state")

        def write(db):
            run = load_run(db, run_id)
            if run.status not in ("RUNNING", "RECONCILING"):
                return run
            return update_run(db, run, status=status, reason=reason, completed_at=utc_datetime(at))

        return await self._transaction(write)

    async def recover(self, lane_id, at):
        at = utc_datetime(at)

        def write(db):
            results = []
            for row in db.execute(
                "SELECT body FROM agent_runs WHERE lane_id=? AND status='RUNNING'", (lane_id,)
            ).fetchall():
                run = AgentRun.model_validate_json(row[0])
                unknown = any(t.status == "SENT" for t in run.turns) or bool(run.tools)
                results.append(
                    update_run(
                        db,
                        run,
                        status="RECONCILING"
                        if unknown
                        else "EXPIRED"
                        if at >= run.deadline
                        else "INTERRUPTED",
                        reason="restart_recovery",
                        completed_at=at,
                    )
                )
            return tuple(results)

        return await self._transaction(write)
