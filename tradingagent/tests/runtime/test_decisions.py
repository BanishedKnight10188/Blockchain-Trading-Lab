"""A slow decision never owns the market sampler or deterministic alert path."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.application.decisions import DecisionService
from agent_platform.application.routing import ModelRouter
from agent_platform.application.sessions import SessionService
from agent_platform.domain.account import AccountSnapshot, TradeBatch
from agent_platform.domain.events import JournalEvent
from agent_platform.domain.overview import OverviewFrame
from agent_platform.domain.routing import RoutingPolicy
from agent_platform.domain.sync import SyncReport
from tests.adapters.test_sqlite_decisions import context as _context
from tests.adapters.test_sqlite_decisions import snapshot
from tests.application.test_advice_queries import assembly
from tests.application.test_decision_service import RecordedModel, Rules
from tests.application.test_routing import tier
from tests.domain.test_decisions import NOW

context = _context


async def runtime(context, *, rules=None, paid=False):
    factory, query, decisions, cache, clock = await assembly(context)
    model = RecordedModel(context[0], clock, wait=True) if paid else None
    service = DecisionService(
        clock=clock,
        router=ModelRouter(
            RoutingPolicy(
                routes=(tier(),) if paid else (),
                daily_limit_usd="1" if paid else "0",
            ),
            clock,
        ),
        decisions=decisions,
        budgets=context[1],
        current=factory,
        model=model,
        rules=rules,
    )
    module = importlib.import_module("agent_platform.runtime.decisions")
    worker = module.DecisionRuntime(context[2], cache, factory, service, clock)
    return worker, query, decisions, cache, clock, model


async def idle(worker):
    async with asyncio.timeout(2):
        task = worker._decision_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_default_has_no_model_or_invented_hold(context):
    worker, query, _, _, _, _ = await runtime(context)
    await worker.tick()
    await idle(worker)
    assert (await query.current()).reasons == ("rule_unconfigured",)
    assert (await query.current()).action is None


@pytest.mark.asyncio
async def test_initial_tick_publishes_explicit_offline_rule_once(context):
    rules = Rules()
    worker, query, _, _, _, _ = await runtime(context, rules=rules)
    for _ in range(10):
        await worker.tick()
    await idle(worker)
    assert rules.calls == 1
    assert (await query.current()).action == "hold"
    with sqlite3.connect(context[0]) as connection:
        assert connection.execute("SELECT count(*) FROM decision_requests").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_only_running_session_can_schedule(context):
    worker, _, _, _, clock, _ = await runtime(context, rules=Rules())
    sessions = SessionService(context[2], clock)
    await sessions.transition("session-1", "paused", 2)
    await worker.tick()
    assert not worker.inflight and (await worker.latest()).session_status == "paused"
    with sqlite3.connect(context[0]) as connection:
        assert connection.execute("SELECT count(*) FROM decision_requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_slow_model_keeps_tick_and_hard_alert_independent(context):
    worker, _, _, cache, clock, model = await runtime(context, paid=True)
    await worker.tick()
    await asyncio.wait_for(model.started.wait(), 2)
    frame = await cache.latest()
    clock.advance_to(NOW + timedelta(seconds=6))
    await cache.publish(frame.model_copy(update={"captured_at": clock.utcnow()}))
    await asyncio.wait_for(worker.tick(), 0.5)
    state = await worker.latest()
    assert worker.inflight and state.alerts[-1].reasons == ("market_quote_stale",)
    assert model.calls == 1 and (await cache.latest()).captured_at == clock.utcnow()
    await worker.stop()


@pytest.mark.asyncio
async def test_stop_cancels_supplier_but_preserves_unknown_bill(context):
    worker, _, decisions, _, _, model = await runtime(context, paid=True)
    await worker.start()
    with pytest.raises(RuntimeError):
        await worker.start()
    await asyncio.wait_for(model.started.wait(), 2)
    await worker.stop()
    await worker.stop()
    assert not worker.running and not worker.inflight
    record = await decisions.latest_decision("session-1")
    usage = record.completion.result.usage
    assert usage.billing_status == "unknown" and not usage.token_counts_known
    assert record.completion.result.reasons == ("provider_cancelled",)


@pytest.mark.asyncio
async def test_pause_cancels_inflight_and_issues_no_second_request(context):
    worker, _, decisions, _, clock, model = await runtime(context, paid=True)
    await worker.tick()
    await asyncio.wait_for(model.started.wait(), 2)
    await SessionService(context[2], clock).transition("session-1", "paused", 2)
    await worker.tick()
    await idle(worker)
    assert model.calls == 1
    assert (
        await decisions.latest_decision("session-1")
    ).completion.result.usage.actual_cost_usd is None


@pytest.mark.asyncio
async def test_persistence_failure_stops_new_decisions_but_not_cache(context):
    worker, _, _, cache, _, _ = await runtime(context, rules=Rules())
    with sqlite3.connect(context[0]) as connection:
        connection.execute("""
            CREATE TRIGGER reject_decision BEFORE INSERT ON journal_events
            BEGIN SELECT RAISE(ABORT, 'private failure must not reflect'); END;
        """)
    await worker.tick()
    await idle(worker)
    assert (await worker.latest()).error == "persistence"
    await worker.tick()
    frame = await cache.latest()
    await cache.publish(frame)
    assert (await cache.latest()).event_id != frame.event_id
    assert "private failure" not in (await worker.latest()).model_dump_json()
    with sqlite3.connect(context[0]) as connection:
        assert connection.execute("SELECT count(*) FROM decision_requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_repeated_pause_ticks_do_not_cancel_unknown_settlement_again(context):
    worker, _, decisions, _, clock, model = await runtime(context, paid=True)
    budgets = worker.decisions.budgets
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingSettlement:
        async def reserve(self, *args, **kwargs):
            return await budgets.reserve(*args, **kwargs)

        async def settle(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await budgets.settle(*args, **kwargs)

    worker.decisions.budgets = WaitingSettlement()
    await worker.tick()
    await asyncio.wait_for(model.started.wait(), 2)
    await SessionService(context[2], clock).transition("session-1", "paused", 2)
    await worker.tick()
    await asyncio.wait_for(entered.wait(), 2)
    await worker.tick()
    await asyncio.sleep(0)
    assert worker.inflight
    release.set()
    await idle(worker)
    record = await decisions.latest_decision("session-1")
    assert record.completion.result.usage.billing_status == "unknown"


@pytest.mark.asyncio
async def test_monitor_read_failure_cancels_existing_supplier_before_publication(context):
    from agent_platform.ports.sessions import PersistenceUnavailable

    worker, _, decisions, _, clock, _ = await runtime(context, paid=True)
    release = asyncio.Event()
    model = RecordedModel(context[0], clock, operation=release.wait)
    worker.decisions.model = model
    await worker.tick()
    await asyncio.wait_for(model.started.wait(), 2)
    read = worker.sessions.active

    async def failed_read():
        raise PersistenceUnavailable("must not reflect private diagnostics")

    worker.sessions.active = failed_read
    await worker.tick()
    worker.sessions.active = read
    release.set()
    await idle(worker)
    result = (await decisions.latest_decision("session-1")).completion.result
    assert result.status != "published" and result.usage.billing_status == "unknown"


@pytest.mark.asyncio
async def test_first_ready_frame_runs_start_request_without_waiting_300_seconds(context):
    rules = Rules()
    worker, query, _, cache, clock, _ = await runtime(context, rules=rules)
    frame = await cache.latest()
    await cache.publish(frame.model_copy(update={"features": None}))
    await worker.tick()
    assert not worker.inflight
    clock.advance_to(NOW + timedelta(seconds=1))
    await cache.publish(frame.model_copy(update={"captured_at": clock.utcnow()}))
    await worker.tick()
    await idle(worker)
    assert rules.calls == 1 and (await query.current()).status == "published"


async def publish_at(context, cache, clock, seconds, *, btc="0", usdt="1000"):
    clock.advance_to(NOW + timedelta(seconds=seconds))
    at = clock.utcnow()
    account = AccountSnapshot(
        account_ref="local-spot",
        as_of=at,
        balances=(
            {"asset": "BTC", "free": btc, "locked": "0"},
            {"asset": "USDT", "free": usdt, "locked": "0"},
        ),
    )
    batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT")
    imported = await context[1].ingest(
        batch,
        account,
        JournalEvent(
            event_id=f"account-at:{seconds}",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=at,
        ),
    )
    value = snapshot(context[-1])
    market = value.market.model_copy(
        update={
            "as_of": at,
            "latest_received_at": at,
            "latest_quote_at": at,
            "book_as_of": at,
        }
    )
    await cache.publish(
        OverviewFrame(
            mode="fake",
            market_source="fake",
            account_source="fake",
            captured_at=at,
            market=market,
            features=value.features.model_copy(update={"as_of": at}),
            sync=SyncReport(
                account=imported.account,
                attempted_at=at,
                next_attempt_at=at + timedelta(seconds=15),
                next_cursor={},
            ),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("holding,period", [(False, 300), (True, 60)])
async def test_runtime_periodic_interval_uses_actual_spot_holding(context, holding, period):
    rules = Rules()
    worker, _, _, cache, clock, _ = await runtime(context, rules=rules)
    btc = "0.01" if holding else "0"
    await publish_at(context, cache, clock, 0, btc=btc)
    await worker.tick()
    await idle(worker)
    assert rules.calls == 1
    await publish_at(context, cache, clock, period - 1, btc=btc)
    await worker.tick()
    await idle(worker)
    assert rules.calls == 1
    await publish_at(context, cache, clock, period, btc=btc)
    await worker.tick()
    await idle(worker)
    assert rules.calls == 2


@pytest.mark.asyncio
async def test_account_refresh_does_not_trigger_but_material_balance_change_does(context):
    rules = Rules()
    worker, _, _, cache, clock, _ = await runtime(context, rules=rules)
    await worker.tick()
    await idle(worker)
    await publish_at(context, cache, clock, 1, usdt="900")
    await worker.tick()
    await idle(worker)
    assert rules.calls == 2
    await publish_at(context, cache, clock, 2, usdt="900")
    await worker.tick()
    await idle(worker)
    assert rules.calls == 2


@pytest.mark.asyncio
async def test_unknown_usage_projects_null_tokens_and_exact_estimate(context):
    worker, query, _, _, _, model = await runtime(context, paid=True)
    await worker.tick()
    await asyncio.wait_for(model.started.wait(), 2)
    await worker.stop()
    view = await query.current()
    assert view.usage.billing_status == "unknown" and view.usage.actual_cost_usd is None
    assert not view.usage.token_counts_known
    assert view.usage.input_tokens is None and view.usage.output_tokens is None
    assert isinstance(view.model_dump(mode="json")["usage"]["estimated_cost_usd"], str)


@pytest.mark.asyncio
async def test_runtime_fault_hides_action_but_preserves_committed_unknown_usage(context):
    from agent_platform.application.queries import QueryService
    from agent_platform.ports.sessions import PersistenceUnavailable

    worker, advice, _, cache, clock, model = await runtime(context, paid=True)
    await worker.tick()
    await asyncio.wait_for(model.started.wait(), 2)
    read = worker.sessions.active

    async def failed_read():
        raise PersistenceUnavailable("private fault")

    worker.sessions.active = failed_read
    await worker.tick()
    worker.sessions.active = read
    await idle(worker)
    view = await QueryService(cache, clock, advice=advice, decision_runtime=worker).overview()
    assert view.advice_status == "unavailable" and view.advice.action is None
    assert view.advice.usage.billing_status == "unknown"
    assert view.advice.usage_status == "recorded"
