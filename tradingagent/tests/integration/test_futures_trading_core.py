"""Small core acceptance set: real SQLite, no network, no paid inference."""

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.application.agent_controls import AgentControlService
from agent_platform.bootstrap_futures import assemble_futures
from agent_platform.config import RuntimeConfig
from agent_platform.domain.agent_controls import AdviceSelection, TraderSelection
from agent_platform.domain.futures_values import FuturesFunding
from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.ports.trading_execution import ExecutionRejected
from agent_platform.web.app import create_app
from tests.adapters.test_futures_paper_store import context
from tests.domain.test_futures_paper import NOW, settings_data
from tests.web.test_paper_routes import headers


@pytest_asyncio.fixture
async def assembled(tmp_path):
    ctx = await context(tmp_path)
    clock = FakeClock(NOW)
    controls = AgentControlService(store=ctx[1], clock=clock)
    async with AsyncExitStack() as stack:
        svc, worker = await assemble_futures(
            config=RuntimeConfig(paper=True, paper_mock=True),
            stack=stack,
            database_path=ctx[5],
            sessions=ctx[2],
            controls=controls,
            clock=clock,
            paper=None,
            history=None,
        )
        await svc.store.acquire_owner()
        svc.ready = True
        try:
            yield svc, clock, ctx, worker
        finally:
            svc.ready = False
            await svc.recover()
            await svc.store.release_owner()


async def configured(svc):
    run = await svc.configure(
        TradingLimits(**settings_data()),
        TradingPolicy(
            order_notional_usdt="2000",
            max_price_drift_bps="20",
            min_confidence="0.8",
            strategy_instructions="Use scripted offline actions.",
        ),
        session_id="s1",
        style_revision=1,
    )
    a = await svc.backend.account(run.scope, svc.clock.utcnow())
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=a.revision,
        style_revision=1,
        trader_revision=1,
    )
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_settlement_past_deadline_preserves_actual_outcome_and_fees(
    assembled, provider_failure
):
    from agent_platform.adapters.sqlite.budgets import SqliteBudgetStore
    from agent_platform.application.decision_models import BudgetedDecisionModel
    from agent_platform.bootstrap_paper import TrialDecisionModel
    from agent_platform.config import FuturesCadence
    from agent_platform.domain.model_diagnostics import ModelDiagnostic
    from agent_platform.domain.model_modules import JevModuleSettings
    from agent_platform.domain.paper_trials import PaperTrialPolicy
    from agent_platform.domain.routing import ModelPrice
    from agent_platform.ports.model import ModelCallFailed

    svc, clock, ctx, _ = assembled
    run = await configured(svc)
    original = svc.model
    budgets = SqliteBudgetStore(ctx[5].with_name("deadline-budget.sqlite3"))
    await budgets.initialize()
    price = ModelPrice(
        version=svc.price_version,
        input_usd_per_million="0.042",
        output_usd_per_million="0",
        verified_at=clock.utcnow(),
    )

    class Port:
        calls = 0

        async def decide(self, request, quote):
            self.calls += 1
            if provider_failure:
                raise ModelCallFailed(
                    "provider_timeout", diagnostic=ModelDiagnostic(stage="transport")
                )
            reply = await original.decide(request)
            return reply.model_copy(
                update={
                    "usage": reply.usage.model_copy(
                        update={
                            "estimated_cost_usd": quote.estimated_cost_usd,
                            "actual_cost_usd": Decimal("0.0000084"),
                        }
                    )
                }
            )

    port = Port()
    model = BudgetedDecisionModel(
        port=port,
        budgets=budgets,
        clock=clock,
        price=price,
        daily_limit_usd="1",
        max_single_cost_usd="0.02",
        settings=JevModuleSettings(enabled=True),
    )
    svc.model = TrialDecisionModel(
        model,
        PaperTrialPolicy(
            trial_total_usd="1",
            single_call_usd="0.02",
            issued_at=clock.utcnow(),
            expires_at=None,
            price=price,
        ),
        clock,
    )
    svc.cadence = FuturesCadence(prediction_ttl_seconds=1)
    settle = budgets.settle

    async def slow_settle(*args, **kwargs):
        result = await settle(*args, **kwargs)
        # The response is already received; delay only its owned local write.
        await asyncio.sleep(1.4)
        clock.advance_to(NOW + timedelta(seconds=2))
        return result

    budgets.settle = slow_settle
    cycle = await svc.step()
    assert port.calls == 1
    assert cycle.reason == ("provider_timeout" if provider_failure else "decision_expired")
    assert cycle.usage is not None
    assert cycle.diagnostic.stage == ("transport" if provider_failure else "binding")
    assert cycle.diagnostic.local_timing.settle_us >= 1_350_000
    balance = await budgets.budget_balance(clock.utcnow(), daily_limit_usd="1")
    account = await svc.backend.account(run.scope, clock.utcnow())
    if provider_failure:
        assert cycle.usage.billing_status == "unknown"
        assert balance.reserved_usd > 0 and balance.spent_usd == 0
        assert account.status == "running" and svc.model.enabled
        assert (await svc.public_view())["model_recovery"]["retrying"]
    else:
        assert cycle.usage.billing_status == "confirmed"
        assert balance.spent_usd == Decimal("0.0000084") and balance.reserved_usd == 0
        assert account.status == "running" and svc.model.enabled
    assert account.quantity == 0
    assert (await svc.store.recent(run.scope.account_ref))[0] == cycle


@pytest.mark.asyncio
async def test_open_reduce_restart_preserves_hand_checked_funds(assembled):
    svc, clock, _, _ = assembled
    run = await configured(svc)
    svc.model.choices = ("OPEN_LONG", "REDUCE")
    first = await svc.step()
    assert first.status == "filled"
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.free_usdt == 599 and a.margin_usdt == 400 and a.quantity == 1
    clock.advance_to(NOW + timedelta(seconds=1))
    svc.maintenance.market.price = Decimal("2100")
    second = await svc.step()
    assert second.status == "filled"
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.free_usdt == Decimal("1097.95") and a.quantity == 0 and a.realized_pnl_usdt == 100
    await svc.recover()
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.status == "paused" and a.free_usdt == Decimal("1097.95")
    assert len(await svc.store.recent(run.scope.account_ref)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "provider_access_denied",
        "provider_error",
        "provider_timeout",
        "provider_transport_error",
        "provider_http_402",
        "provider_http_429",
        "provider_http_503",
        "invalid_model_response",
        "invalid_model_assessment",
        "invalid_model_metadata",
    ],
)
async def test_provider_failure_pauses_calls_but_keeps_position_maintenance(assembled, reason):
    from agent_platform.ports.model import ModelCallFailed

    svc, clock, _, _ = assembled
    run = await configured(svc)
    svc.model.choices = ("OPEN_LONG",)
    assert (await svc.step()).status == "filled"
    clock.advance_to(NOW + timedelta(seconds=1))

    class DeniedModel:
        calls = 0
        enabled = True

        def set_enabled(self, enabled):
            self.enabled = enabled

        async def decide(self, request):
            self.calls += 1
            raise ModelCallFailed(reason)

    svc.model = DeniedModel()
    failure = await svc.step()
    assert failure.status == "rejected" and failure.reason == reason
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.status == "paused" and account.quantity == 1
    assert not svc.model.enabled
    assert await svc.step() is None and svc.model.calls == 1
    clock.advance_to(NOW + timedelta(seconds=2))
    svc.maintenance.market.price = Decimal("1500")
    await svc.maintain()
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.status == "liquidated" and account.quantity == 0
    assert svc.model.calls == 1


@pytest.mark.asyncio
async def test_failed_diagnostic_is_persisted_and_available_on_read(assembled):
    from agent_platform.domain.model_diagnostics import ModelDiagnostic
    from agent_platform.ports.model import ModelCallFailed

    svc, clock, _, _ = assembled
    run = await configured(svc)

    class InvalidModel:
        enabled = True

        def set_enabled(self, enabled):
            self.enabled = enabled

        async def decide(self, request):
            raise ModelCallFailed(
                "invalid_model_assessment",
                diagnostic=ModelDiagnostic(
                    stage="criteria", question_index=0, expected_count=4, actual_count=3
                ),
            )

    svc.model = InvalidModel()
    failed = await svc.step()
    assert failed.diagnostic.stage == "criteria"
    stored = (await svc.store.recent(run.scope.account_ref))[0]
    assert stored.diagnostic == failed.diagnostic
    view = await svc.public_view()
    assert view["cycles"][0]["diagnostic"]["actual_count"] == 3
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "paused"


@pytest.mark.asyncio
async def test_read_only_start_rejected_but_paused_position_maintenance_continues(assembled):
    from agent_platform.bootstrap_paper import PaperModelConfig, TrialDecisionModel
    from agent_platform.web.futures_trading_routes import action_failure_code
    from tests.test_paper_config import config_data

    svc, clock, _, _ = assembled
    run = await configured(svc)
    svc.model.choices = ("OPEN_LONG",)
    assert (await svc.step()).status == "filled"
    account = await svc.backend.account(run.scope, clock.utcnow())
    await svc.pause(account_ref=run.scope.account_ref, expected_revision=account.revision)

    class ForbiddenModel:
        enabled = True

        def set_enabled(self, enabled):
            self.enabled = enabled

        async def decide(self, request):
            pytest.fail("read-only maintenance must not invoke the model")

    svc.model = TrialDecisionModel(
        ForbiddenModel(), PaperModelConfig(**config_data()), clock, read_only=True
    )
    account = await svc.backend.account(run.scope, clock.utcnow())
    with pytest.raises(ValueError) as caught:
        await svc.start(
            account_ref=run.scope.account_ref,
            expected_revision=account.revision,
            style_revision=1,
            trader_revision=1,
        )
    assert action_failure_code(caught.value) == "model_read_only"
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "paused"
    clock.advance_to(NOW + timedelta(seconds=2))
    svc.maintenance.market.price = Decimal("1500")
    await svc.maintain()
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "liquidated"


@pytest.mark.asyncio
async def test_model_budget_rejection_pauses_before_any_execution(assembled):
    from agent_platform.ports.persistence import BudgetExceeded

    svc, clock, _, _ = assembled
    run = await configured(svc)

    class ExhaustedModel:
        calls = 0
        enabled = True

        def set_enabled(self, value):
            self.enabled = value

        async def decide(self, request):
            self.calls += 1
            raise BudgetExceeded("local budget blocked before provider")

    svc.model = ExhaustedModel()
    cycle = await svc.step()
    assert cycle.status == "rejected" and cycle.reason == "model_budget_exhausted"
    assert cycle.usage is None and cycle.command_id is None
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "paused"
    assert not svc.model.enabled and await svc.step() is None
    assert svc.model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["trader", "advice"])
async def test_parallel_advice_toggle_does_not_override_trader_guard(assembled, selection):
    svc, clock, _, _ = assembled
    run = await configured(svc)
    original = svc.model
    original.choices = ("OPEN_SHORT",)

    class ChangedModel:
        async def decide(self, request):
            answer = await original.decide(request)
            if selection == "trader":
                await svc.controls.update_trader(
                    TraderSelection(
                        enabled=False, mode="auto", execution_environment="paper", confirmed=True
                    ),
                    expected_revision=1,
                )
            else:
                await svc.controls.update_advice(
                    AdviceSelection(enabled=True, confirmed=True), expected_revision=1
                )
            return answer

    svc.model = ChangedModel()
    result = await svc.step()
    a = await svc.backend.account(run.scope, clock.utcnow())
    if selection == "trader":
        assert (
            result.status == "rejected" and result.reason == "selection_changed" and a.quantity == 0
        )
        await svc.maintain()
        assert (await svc.backend.account(run.scope, clock.utcnow())).status == "paused"
    else:
        assert result.status == "filled" and a.side == "short" and a.quantity == 1


@pytest.mark.asyncio
async def test_due_funding_blocks_until_published_then_maintains_paused_position(assembled):
    svc, clock, _, _ = assembled
    due = NOW + timedelta(seconds=60)
    market = svc.maintenance.market
    market.next_due = due
    run = await configured(svc)
    svc.model.choices = ("OPEN_LONG",)
    assert (await svc.step()).status == "filled"
    a = await svc.backend.account(run.scope, clock.utcnow())
    await svc.pause(account_ref=run.scope.account_ref, expected_revision=a.revision)
    clock.advance_to(due)
    with pytest.raises(ExecutionRejected, match="funding_publication_pending"):
        await svc.maintenance.maintain(run.scope, run.created_at)
    assert (await svc.store.checkpoint(run.scope.account_ref)).next_due == due
    market.events = (
        FuturesFunding(
            symbol="ETHUSDT", source="offline_replay", rate="0.001", mark="2000", settled_at=due
        ),
    )
    await svc.maintain()
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.status == "paused" and a.margin_usdt == 398 and a.funding_usdt == -2
    await svc.maintain()
    assert (await svc.backend.account(run.scope, clock.utcnow())).funding_usdt == -2
    clock.advance_to(due + timedelta(seconds=1))
    market.price = Decimal("1500")
    await svc.maintain()
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.status == "liquidated" and a.quantity == 0
    assert svc.model.calls == 1


@pytest.mark.asyncio
async def test_second_process_owner_and_fixed_wallet_policy(assembled):
    from agent_platform.adapters.sqlite.trading_runtime import SqliteTradingRuntimeStore
    from agent_platform.application.futures_trading import TradingGuard

    svc, _, ctx, _ = assembled
    await configured(svc)
    another = SqliteTradingRuntimeStore(ctx[5])
    with pytest.raises(RuntimeError, match="another process"):
        await another.acquire_owner()
    with pytest.raises(TradingGuard, match="configured_policy_is_fixed"):
        await svc.configure(
            TradingLimits(**(settings_data() | {"initial_usdt": "2000"})),
            TradingPolicy(
                order_notional_usdt="2000",
                max_price_drift_bps="20",
                min_confidence="0.8",
                strategy_instructions="changed",
            ),
            session_id="s1",
            style_revision=1,
        )


@pytest.mark.parametrize("unavailable", [False, True])
def test_protected_web_configure_start_wait_and_reopen(tmp_path, unavailable):
    path = tmp_path / "web.sqlite3"
    config = RuntimeConfig(paper=True, paper_mock=True, market_archive=False)
    with TestClient(create_app(path, runtime_config=config), base_url="http://127.0.0.1") as client:
        assert client.get("/api/futures-trading").status_code == 403
        auth = headers(client)
        response = client.post(
            "/api/sessions",
            headers=auth,
            json={
                "style_strength": 80,
                "style_confirmed": True,
                "analysis_target": {
                    "market": "usdt_perpetual",
                    "symbol": "ETHUSDT",
                    "history_days": 1,
                },
            },
        )
        assert response.status_code == 201, response.text
        session = response.json()
        if "session" in session:
            session = session["session"]
        r = client.post(
            "/api/controls/trader",
            headers=auth,
            json={
                "enabled": True,
                "mode": "auto",
                "execution_environment": "paper",
                "confirmed": True,
                "expected_revision": 0,
            },
        )
        assert r.status_code == 200, r.text
        app = client.app
        started = client.put(
            f"/api/sessions/{session['session_id']}/state",
            headers=auth,
            json={"status": "running", "expected_revision": session["revision"]},
        )
        assert started.status_code == 200, started.text
        assert started.json()["session"]["status"] == "running"
        assert started.json()["session"]["analysis_target"] == session["analysis_target"]
        r = client.post(
            "/api/futures-trading/configure",
            headers=auth,
            json={
                "confirmed": True,
                "session_id": session["session_id"],
                "style_revision": 1,
                "limits": settings_data(),
                "policy": {
                    "order_notional_usdt": "100",
                    "max_price_drift_bps": "20",
                    "min_confidence": "0.8",
                    "strategy_instructions": "Wait in offline validation.",
                },
            },
        )
        assert r.status_code == 200, r.text
        a = r.json()["account"]
        body = {
            "account_ref": a["scope"]["account_ref"],
            "expected_revision": a["revision"],
            "style_revision": 1,
            "trader_revision": r.json()["trader_revision"],
            "confirmed": True,
        }
        if unavailable:
            from agent_platform.ports.futures_market import FuturesMarketUnavailable

            market = app.state.services.futures_trading.maintenance.market
            original_snapshot = market.snapshot

            async def unavailable_snapshot(symbol):
                raise FuturesMarketUnavailable("futures_quote_unavailable")

            market.snapshot = unavailable_snapshot
            try:
                r = client.post("/api/futures-trading/start", headers=auth, json=body)
            finally:
                market.snapshot = original_snapshot
            assert r.status_code == 409 and r.json()["detail"]
            assert "market_unavailable" in r.json()["detail"]
            assert client.get("/api/futures-trading").json()["account"]["status"] == "paused"
            assert app.state.services.futures_trading.model.calls == 0
            return
        r = client.post("/api/futures-trading/start", headers=auth, json=body)
        assert r.status_code == 200, r.text

        async def activated():
            for _ in range(40):
                cycles = (await app.state.services.futures_trading.public_view())["cycles"]
                if cycles and cycles[0]["status"] != "pending":
                    return
                await asyncio.sleep(0.05)
            raise AssertionError("first activation did not wake the decision worker")

        client.portal.call(activated)
        view = client.get("/api/futures-trading").json()
        assert (
            view["cycles"][0]["status"] == "wait" and Decimal(view["account"]["free_usdt"]) == 1000
        )
        assert not view["real_orders_enabled"] and not view["paid_models_enabled"]
        assert "futures-configure" in client.get("/jev-trader").text
    with TestClient(create_app(path, runtime_config=config), base_url="http://127.0.0.1") as client:
        headers(client)
        view = client.get("/api/futures-trading").json()
        assert (
            view["account"]["status"] == "paused" and Decimal(view["account"]["free_usdt"]) == 1000
        )
        assert view["cycles"][0]["status"] == "wait"


@pytest.mark.asyncio
async def test_transient_market_failure_before_decision_preserves_running_wallet(assembled):
    from agent_platform.ports.futures_market import FuturesMarketUnavailable

    svc, clock, _, _ = assembled
    run = await configured(svc)

    async def unavailable(*args):
        raise FuturesMarketUnavailable("futures_quote_unavailable")

    svc.maintenance.maintain = unavailable
    assert await svc.step() is None
    assert svc.last_failure == "market_unavailable" and svc.model.calls == 0
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.status == "running" and account.free_usdt == 1000


@pytest.mark.asyncio
async def test_slow_model_does_not_block_liquidation_and_old_answer_is_discarded(assembled):
    svc, clock, _, _ = assembled
    run = await configured(svc)
    original = svc.model
    original.choices = ("OPEN_LONG",)
    assert (await svc.step()).status == "filled"
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowModel:
        async def decide(self, request):
            entered.set()
            await release.wait()
            return await original.decide(request)

    svc.model = SlowModel()
    task = asyncio.create_task(svc.step())
    await asyncio.wait_for(entered.wait(), 2)
    clock.advance_to(NOW + timedelta(seconds=1))
    svc.maintenance.market.price = Decimal("1500")
    await asyncio.wait_for(svc.maintain(), 2)
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "liquidated"
    release.set()
    result = await task
    assert result.status == "rejected" and result.reason == "account_changed"
    assert (await svc.backend.account(run.scope, clock.utcnow())).quantity == 0


@pytest.mark.asyncio
async def test_final_quote_drift_cannot_spend_wallet(assembled):
    svc, clock, _, _ = assembled
    run = await configured(svc)
    original = svc.model
    original.choices = ("OPEN_LONG",)

    class DriftingModel:
        async def decide(self, request):
            response = await original.decide(request)
            svc.maintenance.market.price = Decimal("2200")
            return response

    svc.model = DriftingModel()
    result = await svc.step()
    assert result.status == "rejected"
    a = await svc.backend.account(run.scope, clock.utcnow())
    assert a.quantity == 0 and a.free_usdt == 1000


@pytest.mark.asyncio
async def test_failed_worker_ownership_cleanup_cannot_pause_the_owner(assembled):
    from agent_platform.adapters.sqlite.trading_runtime import SqliteTradingRuntimeStore
    from agent_platform.runtime.futures_trading import FuturesTradingRuntime

    svc, clock, ctx, _ = assembled
    run = await configured(svc)
    contender = type(svc)(
        sessions=svc.sessions,
        controls=svc.controls,
        store=SqliteTradingRuntimeStore(ctx[5]),
        backend=svc.backend,
        maintenance=svc.maintenance,
        execution=svc.execution,
        model=svc.model,
        history=svc.history,
        clock=clock,
        market_source=svc.market_source,
        decision_source=svc.decision_source,
        price_version=svc.price_version,
    )
    worker = FuturesTradingRuntime(contender)
    with pytest.raises(RuntimeError):
        await worker.start()
    await worker.stop()
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "running"


@pytest.mark.asyncio
async def test_quote_is_visible_before_wallet_configuration(assembled):
    svc, _, _, _ = assembled
    await svc.maintain()
    view = await svc.public_view()
    assert view["account"] is None and view["quote"]["quote"]["symbol"] == "ETHUSDT"
    assert view["quote_fresh"] and not view["paid_models_enabled"]


@pytest.mark.asyncio
async def test_expired_model_policy_pauses_before_any_call(assembled):
    svc, clock, _, _ = assembled
    run = await configured(svc)

    class ExpiredModel:
        enabled = True

        def validate_active(self):
            raise ValueError("expired")

        def set_enabled(self, enabled):
            self.enabled = enabled

        async def decide(self, request):
            raise AssertionError("expired model must never be called")

    svc.model = ExpiredModel()
    assert await svc.step() is None
    assert (await svc.backend.account(run.scope, clock.utcnow())).status == "paused"
    assert not svc.model.enabled


@pytest.mark.asyncio
async def test_lost_execution_writeback_is_unknown_and_recovers_without_second_fill(assembled):
    svc, clock, _, _ = assembled
    run = await configured(svc)
    svc.model.choices = ("OPEN_LONG",)
    original = svc.execution.journal.save
    failed = False

    async def lost(receipt, expected_revision, **kwargs):
        nonlocal failed
        if receipt.status == "filled" and not failed:
            failed = True
            raise OSError("offline lost writeback")
        return await original(receipt, expected_revision, **kwargs)

    svc.execution.journal.save = lost
    result = await svc.step()
    assert result.status == "unknown"
    assert (await svc.backend.account(run.scope, clock.utcnow())).quantity == 1
    assert await svc.step() is None
    view = await svc.public_view()
    assert view["cycles"][0]["execution"]["status"] == "filled"
    assert (
        svc.model.calls == 1
        and (await svc.backend.account(run.scope, clock.utcnow())).free_usdt == 599
    )
