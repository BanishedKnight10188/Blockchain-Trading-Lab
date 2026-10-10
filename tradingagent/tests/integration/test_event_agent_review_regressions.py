"""Review regressions exercise persisted facts and real Paper transitions."""

import asyncio
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.agent_tools import ToolCall
from tests.application.test_agent_orchestrator import Model, setup
from tests.fixtures.event_agent_cases import grant, response
from tests.fixtures.watch_cases import NOW, definition, frame
from tests.integration.test_event_agent_paper import prepared


@pytest.mark.asyncio
async def test_funding_before_first_entry_does_not_block_stop(tmp_path):
    from agent_platform.domain.futures_market import FuturesFundingWindow

    lane, _, _, backend, _, intents, call, run, market, clock, guardian = await prepared(tmp_path)
    clock.advance_to(NOW + timedelta(seconds=10))
    await intents.execute_tool(call, run, lane)

    async def settlements(symbol, *, after, through):
        at = NOW + timedelta(seconds=5)
        return FuturesFundingWindow(
            symbol=symbol,
            source="offline_replay",
            requested_after=after,
            requested_through=through,
            captured_at=through,
            events=[]
            if after >= at
            else [
                {
                    "symbol": symbol,
                    "source": "offline_replay",
                    "settled_at": at,
                    "rate": "0.001",
                    "mark": "105",
                }
            ],
        )

    market.settlements = settlements
    clock.advance_to(NOW + timedelta(seconds=11))
    market.price = "90"
    status = await guardian.step(lane.scope)
    assert status.status == "ready"
    assert (await backend.account(lane.scope, clock.utcnow())).quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "blocked"])
async def test_funding_failure_still_executes_stop(tmp_path, failure):
    lane, _, _, backend, _, intents, call, run, market, clock, guardian = await prepared(tmp_path)
    await intents.execute_tool(call, run, lane)

    async def settlements(*args, **kwargs):
        if failure == "unavailable":
            raise OSError("funding temporarily unavailable")
        await asyncio.Event().wait()

    market.settlements = settlements
    clock.advance_to(NOW + timedelta(seconds=1))
    market.price = "90"
    status = await asyncio.wait_for(guardian.step(lane.scope), 2)
    assert status.status == "degraded"
    assert (await backend.account(lane.scope, clock.utcnow())).quantity == 0


@pytest.mark.asyncio
async def test_late_opening_fill_remains_protected_after_first_reduction(tmp_path):
    from agent_platform.adapters.sqlite.event_paper import save_wallet, wallet
    from agent_platform.domain.futures_paper import FuturesPaperOrder
    from agent_platform.domain.futures_paper_engine import execute
    from agent_platform.domain.trading_execution import ExecutionReceipt

    (
        lane,
        runs,
        protections,
        backend,
        execution,
        intents,
        call,
        run,
        market,
        clock,
        guardian,
    ) = await prepared(tmp_path)

    class Partial:
        receipt = None

        async def fill(self, command, quantity, quote, at, status):
            def commit():
                with closing(backend._connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    before = wallet(db, lane.scope.account_ref)
                    transition = execute(
                        before, FuturesPaperOrder(action="open_long", quantity=quantity), quote, at
                    )
                    save_wallet(db, transition.state)
                    old = self.receipt
                    total = Decimal(quantity) + (old.filled_quantity if old else 0)
                    self.receipt = ExecutionReceipt(
                        command=command,
                        status=status,
                        filled_quantity=total,
                        average_price=(
                            ((old.average_price * old.filled_quantity) if old else 0)
                            + transition.operation.price * Decimal(quantity)
                        )
                        / total,
                        fee_usdt=transition.operation.fee_usdt + (old.fee_usdt if old else 0),
                        backend_order_id="partial-review",
                        backend_at=at,
                        observed_at=at,
                    )

            await backend._io(commit)

        async def submit(self, command, quote, at):
            if command.action == "reduce":
                return await backend.submit(command, quote, at)
            await self.fill(command, "0.4", quote, at, "partially_filled")
            raise OSError("crash after first fill")

        async def lookup(self, command, at):
            if command.action == "reduce":
                return await backend.lookup(command, at)
            return self.receipt.model_copy(update={"observed_at": at})

    partial = Partial()
    execution.execution = partial
    result = await intents.execute_tool(call, run, lane)
    await runs.save_lane(lane.model_copy(update={"revision": 2, "enabled": False}), 1)
    clock.advance_to(NOW + timedelta(seconds=1))
    market.price = "90"
    await guardian.step(lane.scope)
    assert (await backend.account(lane.scope, clock.utcnow())).quantity == 0
    assert await protections.recover(lane.scope), "nonterminal opening retains ownership"

    clock.advance_to(NOW + timedelta(seconds=2))
    await partial.fill(
        partial.receipt.command,
        "0.6",
        (await market.snapshot(lane.symbol)).quote,
        clock.utcnow(),
        "filled",
    )
    # A previous closing receipt is reconciled before a fresh reduction is issued.
    for seconds in (3, 4):
        clock.advance_to(NOW + timedelta(seconds=seconds))
        await guardian.step(lane.scope)
    assert (await backend.account(lane.scope, clock.utcnow())).quantity == 0
    protection = await protections.get(
        result["execution"]["command"]["decision_evidence"]["intent_id"]
    )
    assert protection.filled_quantity == 1 and protection.status == "closed"


async def queued_event(watches, lane):
    from agent_platform.domain.watch_rules import evaluate_watch

    watch = await watches.create(definition(session_id=lane.session_id))
    return await watches.commit_evaluation(
        watch.definition.watch_id, watch.revision, frame(), evaluate_watch(watch, frame(), NOW)
    )


@pytest.mark.asyncio
async def test_busy_analysis_preserves_event_for_immediate_review(tmp_path):
    from agent_platform.runtime.event_agent import EventAgentRuntime

    lane, runs, watches, events, model, agent, _ = await setup(tmp_path)
    analysis = await runs.claim_analysis(lane, "active", NOW)
    watch = await queued_event(watches, lane)
    runtime = EventAgentRuntime(
        lane_id=lane.lane_id, runs=runs, events=events, orchestrator=agent, clock=agent.clock
    )
    assert await runtime.step() is None
    delivery = await events.delivery(watch.event_id)
    assert delivery.status == "QUEUED" and delivery.attempts == 0
    await runs.finish(analysis.run_id, response().final, NOW)
    assert (await runtime.step()).status == "WAIT"
    assert (await events.delivery(watch.event_id)).status == "COMPLETED"


@pytest.mark.asyncio
async def test_watch_cancellation_before_dispatch_releases_lane(tmp_path):
    lane, runs, watches, events, model, agent, _ = await setup(tmp_path)
    watch = await queued_event(watches, lane)
    lease = await events.claim(lane.lane_id, NOW, 120)
    dispatch = events.mark_dispatched

    async def cancel_first(*args):
        await watches.cancel(watch.definition.watch_id, watch.revision, NOW)
        return await dispatch(*args)

    events.mark_dispatched = cancel_first
    result = await agent.review(lease, lane)
    assert result.status == "INTERRUPTED" and not result.turns
    assert not model.calls
    assert (await runs.claim_analysis(lane, "after-cancel", NOW)).status == "RUNNING"


@pytest.mark.asyncio
async def test_task_cancellation_before_dispatch_does_not_strand_lane(tmp_path):
    lane, runs, watches, events, model, agent, _ = await setup(tmp_path)
    await queued_event(watches, lane)
    lease = await events.claim(lane.lane_id, NOW, 120)
    entered = asyncio.Event()

    async def blocked_dispatch(*args):
        entered.set()
        await asyncio.Event().wait()

    events.mark_dispatched = blocked_dispatch
    task = asyncio.create_task(agent.review(lease, lane))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await runs.recent(lane.lane_id))[0].status == "INTERRUPTED"
    assert (await runs.claim_analysis(lane, "after-stop", NOW)).status == "RUNNING"


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [123, None, [], {}])
async def test_malformed_tool_is_archived_rejection_and_run_finishes(tmp_path, wrong):
    class Malformed(Model):
        async def turn(self, request):
            self.calls.append(request)
            if len(self.calls) == 1:
                return response(
                    request.request_id,
                    calls=(
                        ToolCall(
                            tool_call_id="bad-type",
                            name="create_watch",
                            arguments={
                                "timeframe": "1m",
                                "hypothesis": "bad input",
                                "expires_at": wrong,
                                "trigger": {
                                    "logic": "ALL",
                                    "conditions": [
                                        {"metric": "candle.close", "op": "GT", "value": "104"}
                                    ],
                                },
                            },
                        ),
                    ),
                )
            return response(request.request_id)

    lane, runs, watches, _, _, agent, _ = await setup(tmp_path, Malformed())
    run = await agent.analyze(lane, "malformed")
    assert run.status == "WAIT"
    assert run.tools[0].result.status == "rejected"
    assert await watches.list_active(lane.lane_id) == ()


@pytest.mark.asyncio
async def test_agent_hourly_allowance_does_not_count_jev_calls(tmp_path):
    from agent_platform.adapters.sqlite.budgets import SqliteBudgetStore
    from agent_platform.domain.costs import BudgetRequest
    from agent_platform.ports.persistence import HourlyCallLimitExceeded

    budgets = SqliteBudgetStore(tmp_path / "fees.sqlite")
    await budgets.initialize()

    def request(request_id, route_id):
        return BudgetRequest(
            request_id=request_id,
            route_id=route_id,
            purpose="advisory",
            price_version="test-price",
            estimated_cost_usd="0.001",
            daily_limit_usd="0.02",
            hourly_call_limit=1,
            requested_at=NOW,
        )

    await budgets.reserve(request("jev-1", "jev"))
    await budgets.reserve_agent(request("agent-1", "event-agent"), grant(hourly_call_limit=1))
    with pytest.raises(HourlyCallLimitExceeded):
        await budgets.reserve_agent(request("agent-2", "event-agent"), grant(hourly_call_limit=1))
    assert (await budgets.budget_balance(NOW, daily_limit_usd="0.02")).reserved_usd == Decimal(
        "0.002"
    )


@pytest.mark.asyncio
async def test_duplicate_analysis_does_not_retire_active_owner(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def turn(self, request):
            entered.set()
            await release.wait()
            return await super().turn(request)

    lane, runs, _, _, model, agent, _ = await setup(tmp_path, Slow())
    first = asyncio.create_task(agent.analyze(lane, "same-request"))
    await entered.wait()
    duplicate = await agent.analyze(lane, "same-request")
    assert duplicate.status == "RUNNING"
    assert (await runs.get(duplicate.run_id)).status == "RUNNING"
    release.set()
    assert (await first).status == "WAIT"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_cancel_during_claim_cleans_committed_run(tmp_path):
    lane, runs, _, _, _, agent, _ = await setup(tmp_path)
    committed, release = asyncio.Event(), asyncio.Event()
    claim = runs.claim_analysis

    async def after_commit(*args, **kwargs):
        run = await claim(*args, **kwargs)
        committed.set()
        await release.wait()
        return run

    runs.claim_analysis = after_commit
    task = asyncio.create_task(agent.analyze(lane, "cancel-claim"))
    await committed.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await runs.recent(lane.lane_id))[0].status == "INTERRUPTED"


@pytest.mark.asyncio
async def test_preview_does_not_liquidate_or_write_wallet(tmp_path):
    from tests.fixtures.event_agent_cases import request

    lane, runs, _, backend, _, intents, opening, run, market, clock, _ = await prepared(tmp_path)
    await intents.execute_tool(opening, run, lane)
    before = await backend.get(lane.scope.account_ref)
    count = len(await backend.recent(lane.scope))
    call = ToolCall(
        tool_call_id="preview-2",
        name="preview_trade",
        arguments={"action": "reduce", "quantity": "0.4"},
    )
    req = request(run_id=run.run_id, request_id=run.run_id + ":2")
    run = await runs.record_turn(run.run_id, req, None, "SENT")
    run = await runs.record_turn(
        run.run_id, req, response(req.request_id, calls=(call,)), "RESPONDED"
    )
    run = await runs.begin_tool(run.run_id, call)
    clock.advance_to(NOW + timedelta(seconds=1))
    market.price = "1"
    await intents.execute_tool(call, run, lane)
    assert await backend.get(lane.scope.account_ref) == before
    assert len(await backend.recent(lane.scope)) == count
