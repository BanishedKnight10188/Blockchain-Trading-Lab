"""Failure windows use real SQLite and Paper facts with controlled boundary failures."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.fixtures.watch_cases import NOW
from tests.integration.test_event_agent_paper import prepared
from tests.integration.test_futures_trading_core import assembled as assembled
from tests.integration.test_futures_trading_core import configured


@pytest.mark.asyncio
async def test_partial_fill_crash_restores_protection(tmp_path):
    from contextlib import closing

    from agent_platform.adapters.sqlite.event_paper import save_wallet, wallet
    from agent_platform.domain.futures_paper import FuturesPaperOrder
    from agent_platform.domain.futures_paper_engine import execute
    from agent_platform.domain.trading_execution import ExecutionReceipt

    (
        value,
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
        calls = 0

        async def submit(self, command, quote, at):
            if command.action == "reduce":
                return await backend.submit(command, quote, at)
            self.calls += 1

            def commit():
                with closing(backend._connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    before = wallet(db, value.scope.account_ref)
                    transition = execute(
                        before, FuturesPaperOrder(action="open_long", quantity="0.4"), quote, at
                    )
                    save_wallet(db, transition.state)
                    self.receipt = ExecutionReceipt(
                        command=command,
                        status="partially_filled",
                        filled_quantity="0.4",
                        average_price=transition.operation.price,
                        fee_usdt=transition.operation.fee_usdt,
                        backend_order_id="partial-1",
                        backend_at=at,
                        observed_at=at,
                    )
                    backend._archive(
                        db,
                        command.command_id,
                        transition.state,
                        before,
                        transition.operation,
                        quote,
                        self.receipt,
                    )

            await backend._io(commit)
            raise OSError("crash after actual partial fill")

        async def lookup(self, command, at):
            if command.action == "reduce":
                return await backend.lookup(command, at)
            return self.receipt.model_copy(update={"observed_at": at})

    partial = Partial()
    execution.execution = partial
    result = await intents.execute_tool(call, run, value)
    assert result["execution"]["receipt"]["status"] == "unknown"
    assert (await protections.recover(value.scope))[0].filled_quantity == 0
    await runs.save_lane(value.model_copy(update={"revision": 2, "enabled": False}), 1)
    clock.advance_to(NOW + timedelta(seconds=1))
    market.price = "90"
    status = await guardian.step(value.scope)
    assert status.status == "ready"
    assert (await backend.account(value.scope, clock.utcnow())).quantity == 0
    assert (
        await protections.get(result["execution"]["command"]["decision_evidence"]["intent_id"])
    ).filled_quantity == Decimal("0.4")
    assert partial.calls == 1


@pytest.mark.asyncio
async def test_cancel_or_latest_invalidation_blocks_new_exposure(tmp_path):
    from agent_platform.adapters.sqlite.watches import SqliteWatchStore

    (
        value,
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
    watches = SqliteWatchStore(runs.path)
    watch = await watches.get(run.event.watch_id)
    await watches.cancel(watch.definition.watch_id, watch.revision, NOW)
    result = await intents.execute_tool(call, run, value)
    assert result["execution"]["receipt"]["status"] == "rejected"
    assert len(await backend.recent(value.scope)) == 0


@pytest.mark.asyncio
async def test_persisted_post_trigger_conflict_blocks_submit(tmp_path):
    from agent_platform.adapters.sqlite.watches import SqliteWatchStore
    from agent_platform.domain.watch_rules import evaluate_watch
    from tests.fixtures.watch_cases import frame

    (
        value,
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
    watches = SqliteWatchStore(runs.path)
    watch = await watches.get(run.event.watch_id)
    original = frame()
    conflict = frame(tuple(c.model_copy(update={"volume": c.volume + 1}) for c in original.candles))
    await watches.commit_evaluation(
        watch.definition.watch_id, watch.revision, conflict, evaluate_watch(watch, conflict, NOW)
    )
    result = await intents.execute_tool(call, run, value)
    assert result["execution"]["receipt"]["status"] == "rejected"
    assert len(await backend.recent(value.scope)) == 0


@pytest.mark.asyncio
async def test_full_backup_restores_execution_and_protection(tmp_path):
    from agent_platform.adapters.sqlite.backup import backup_database
    from agent_platform.adapters.sqlite.position_protection import SqliteProtectionStore
    from agent_platform.adapters.sqlite.trading_execution import SqliteExecutionJournal

    (
        value,
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
    result = await intents.execute_tool(call, run, value)
    path = tmp_path / "verified.bak"
    report = await backup_database(runs.path, path, kind="core")
    restored = SqliteProtectionStore(path)
    await restored.initialize()
    assert report["integrity"] == "ok"
    assert (await restored.recover(value.scope))[0].filled_quantity == 1
    assert (
        await SqliteExecutionJournal(path).get(result["execution"]["command"]["command_id"])
    ).receipt.status == "filled"


@pytest.mark.asyncio
async def test_slow_model_does_not_block_jev_or_guardian(tmp_path, assembled):
    from tests.application.test_agent_orchestrator import Model, setup

    gate, entered = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def turn(self, request):
            entered.set()
            await gate.wait()
            return await super().turn(request)

    (
        value,
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
    ) = await prepared(tmp_path / "wallet")
    await intents.execute_tool(call, run, value)
    lane2, _, _, _, _, agent, _ = await setup(tmp_path / "research", Slow())
    pending = asyncio.create_task(agent.analyze(lane2, "slow"))
    await entered.wait()
    jev, _, _, _ = assembled
    await configured(jev)
    jev.model.choices = ("OPEN_LONG",)

    clock.advance_to(NOW + timedelta(seconds=1))
    market.price = "90"
    jev_cycle, _ = await asyncio.wait_for(asyncio.gather(jev.step(), guardian.step(value.scope)), 2)
    assert jev_cycle.status == "filled" and not pending.done()
    assert (await backend.account(value.scope, clock.utcnow())).quantity == 0
    gate.set()
    await pending
