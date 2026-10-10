"""Bounded model/tool loop with durable dispatch markers and revision checks."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from agent_platform.domain.event_agent import AgentMessage, AgentTurnRequest
from agent_platform.ports.event_agent import RunAlreadyStarted
from agent_platform.ports.model import ModelCallFailed
from agent_platform.ports.persistence import BudgetExceeded, BudgetFrozen, HourlyCallLimitExceeded
from agent_platform.ports.sessions import PersistenceUnavailable


class AgentOrchestrator:
    def __init__(self, *, runs, events, context, tools, model, route, clock):
        self.runs, self.events, self.context, self.tools, self.model, self.route, self.clock = (
            runs,
            events,
            context,
            tools,
            model,
            route,
            clock,
        )

    async def analyze(self, lane, request_id=None):
        executor_id = uuid4().hex
        run = await self._claim_run(
            self.runs.claim_analysis(
                lane, request_id or uuid4().hex, self.clock.utcnow(), executor_id=executor_id
            ),
            executor_id,
        )
        if run.executor_id != executor_id:
            return run
        return await self._run(run)

    async def review(self, lease, lane):
        executor_id = uuid4().hex
        try:
            run = await self._claim_run(
                self.runs.claim(lease.event, lane, self.clock.utcnow(), executor_id=executor_id),
                executor_id,
                lease,
            )
        except ValueError:
            await self._release_lease(lease)
            return None
        if run.status != "RUNNING" or run.executor_id != executor_id:
            return run
        try:
            await self.events.mark_dispatched(lease, run.run_id, self.clock.utcnow())
        except asyncio.CancelledError:
            await asyncio.shield(self._retire_before_send(run, lease))
            raise
        except (ValueError, OSError, TimeoutError, PersistenceUnavailable):
            return await self._retire_before_send(run, lease)
        run = await self._run(run)
        if run.status in ("WAIT", "COMPLETED", "FAILED", "EXPIRED", "INTERRUPTED"):
            await self.events.complete(lease, run.run_id, self.clock.utcnow())
        else:
            await self.events.fail(lease, "run_outcome_unknown", False, self.clock.utcnow())
        return run

    async def _release_lease(self, lease):
        delivery = await self.events.delivery(lease.event.event_id)
        if delivery.status == "LEASED":
            if self.clock.utcnow() < delivery.lease_until:
                await self.events.release(lease, self.clock.utcnow())
            else:
                await self.events.recover(self.clock.utcnow())

    async def _retire_before_send(self, run, lease=None):
        latest = await self.runs.get(run.run_id)
        unknown = bool(latest.turns or latest.tools)
        retired = await self.runs.abort(
            run.run_id,
            "RECONCILING" if unknown else "INTERRUPTED",
            "dispatch_not_completed",
            self.clock.utcnow(),
        )
        if lease is not None:
            delivery = await self.events.delivery(lease.event.event_id)
            if delivery.status == "LEASED":
                await self._release_lease(lease)
            elif delivery.status == "DISPATCHED" and not unknown:
                await self.events.complete(lease, run.run_id, self.clock.utcnow())
        return retired

    async def _claim_run(self, operation, executor_id, lease=None):
        # Shield the DB boundary so cancellation cannot lose the committed ID.
        claim = asyncio.create_task(operation)
        try:
            return await asyncio.shield(claim)
        except asyncio.CancelledError:
            run = await claim
            if run.executor_id == executor_id:
                await asyncio.shield(self._retire_before_send(run, lease))
            raise

    async def _allowed(self, run):
        if (await self.runs.lane(run.lane.lane_id)) != run.lane or not run.lane.enabled:
            return False
        if run.event is not None:
            watch = await self.context.watches.get(run.event.watch_id)
            return (
                watch.state == "TRIGGERED"
                and watch.definition.version == run.event.definition_revision
                and self.clock.utcnow() < run.event.expires_at
                and await self.context.watches.partition_healthy(run.event.watch_id)
            )
        return True

    async def _run(self, run):
        if run.status != "RUNNING":
            return run
        if run.context or run.turns or run.tools:
            return run
        try:
            async with asyncio.timeout(
                max(0, (run.deadline - self.clock.utcnow()).total_seconds())
            ):
                if self.model.paid and getattr(self.model, "grant", None) is None:
                    return await self.runs.abort(
                        run.run_id, "FAILED", "model_read_only", self.clock.utcnow()
                    )
                ctx = (
                    await self.context.for_event(run.event, run.lane)
                    if run.event
                    else await self.context.for_analysis(run.lane)
                )
                run = await self.runs.set_context(run.run_id, ctx)
                messages = [
                    AgentMessage(
                        role="system",
                        content=(
                            "Use only the available tools. Facts can be unavailable. Analysis "
                            "creates watches, "
                            "review may propose Paper intents. Return JSON action "
                            "WAIT/OBSERVE/TRADE and reason. "
                            "Trade fills are determined only by execution receipts."
                        ),
                    ),
                    AgentMessage(role="user", content=ctx.model_dump_json()),
                ]
                for turn in range(4):
                    if not await self._allowed(run):
                        return await self.runs.abort(
                            run.run_id, "INTERRUPTED", "lane_changed", self.clock.utcnow()
                        )
                    now = self.clock.utcnow()
                    request = AgentTurnRequest(
                        request_id=f"{run.run_id}:turn:{turn + 1}",
                        run_id=run.run_id,
                        lane_id=run.lane.lane_id,
                        captured_at=now,
                        deadline=min(now + timedelta(seconds=30), run.deadline),
                        route=self.route,
                        context_hash=ctx.context_hash,
                        messages=tuple(messages),
                        tools=self.tools.available(run.mode),
                    )
                    run = await self.runs.record_turn(run.run_id, request, None, "SENT")
                    async with asyncio.timeout((request.deadline - now).total_seconds()):
                        response = await self.model.turn(request)
                    run = await self.runs.record_turn(run.run_id, request, response, "RESPONDED")
                    if not await self._allowed(run):
                        return await self.runs.abort(
                            run.run_id, "INTERRUPTED", "lane_changed", self.clock.utcnow()
                        )
                    if response.final:
                        return await self.runs.finish(
                            run.run_id, response.final, self.clock.utcnow()
                        )
                    messages.append(
                        AgentMessage(
                            role="assistant",
                            tool_calls=response.tool_calls,
                            reasoning_details=response.reasoning_details,
                        )
                    )
                    for call in response.tool_calls:
                        if len(run.tools) >= 8:
                            return await self.runs.abort(
                                run.run_id, "FAILED", "tool_limit", self.clock.utcnow()
                            )
                        run = await self.runs.begin_tool(run.run_id, call)
                        result = await self.tools.execute(call, run, run.lane)
                        run = await self.runs.record_tool(run.run_id, call, result)
                        messages.append(
                            AgentMessage(
                                role="tool",
                                tool_call_id=call.tool_call_id,
                                content=result.model_dump_json(),
                            )
                        )
                return await self.runs.abort(
                    run.run_id, "FAILED", "model_request_limit", self.clock.utcnow()
                )
        except asyncio.CancelledError:
            latest = await self.runs.get(run.run_id)
            await asyncio.shield(
                self.runs.abort(
                    run.run_id,
                    "RECONCILING" if latest.turns or latest.tools else "INTERRUPTED",
                    "runtime_stopped",
                    self.clock.utcnow(),
                )
            )
            raise
        except RunAlreadyStarted:
            return await self.runs.get(run.run_id)
        except (BudgetExceeded, BudgetFrozen, HourlyCallLimitExceeded) as error:
            return await self.runs.abort(
                run.run_id, "FAILED", type(error).__name__, self.clock.utcnow()
            )
        except (
            ValueError,
            OSError,
            TimeoutError,
            ModelCallFailed,
            PersistenceUnavailable,
        ) as error:
            latest = await self.runs.get(run.run_id)
            unknown = any(t.status == "SENT" for t in latest.turns) or any(
                t.result is None for t in latest.tools
            )
            return await self.runs.abort(
                run.run_id,
                "RECONCILING" if unknown else "FAILED",
                str(error)[:256] or type(error).__name__,
                self.clock.utcnow(),
            )
