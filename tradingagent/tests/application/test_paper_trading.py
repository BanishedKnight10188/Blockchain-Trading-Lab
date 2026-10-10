"""Independent Paper decisions use fresh quotes and user hard limits."""

import asyncio
import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from tests.adapters.test_paper_store import context as _context
from tests.domain.test_paper_trading import NOW

context = _context


def service(context, choices=("BUY", "SELL", "WAIT")):
    module = importlib.import_module("agent_platform.application.paper_trading")
    fake = importlib.import_module("agent_platform.adapters.fake.paper_trading")
    execution = importlib.import_module("agent_platform.adapters.paper.trading")
    _, _, store, _, sessions, _, _ = context
    clock = FakeClock(NOW)
    market = fake.DemoPaperMarket(clock)
    model = fake.MockPaperDecisionModel(clock, choices=choices)
    return (
        module.PaperTradingService(
            store=store,
            sessions=sessions,
            clock=clock,
            market=market,
            model=model,
            execution=execution.PaperExecution(),
            price_version="offline-mock-v1",
            decision_source="offline_mock",
            market_source="offline_demo",
        ),
        clock,
        market,
        model,
    )


@pytest.mark.asyncio
async def test_independent_buy_sell_wait_and_fee_ledger(context):
    svc, clock, _, model = service(context)
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    cycles = []
    for i in range(3):
        clock.advance_to(NOW + timedelta(seconds=60 * i))
        cycles.append(await svc.step())
    assert [c.status for c in cycles] == ["filled", "filled", "wait"]
    assert [c.decision for c in cycles] == ["BUY", "SELL", "WAIT"]
    account = await svc.store.latest()
    assert account.btc == 0 and account.usdt < 1000
    assert model.calls == 3
    assert (await svc.public_view())["market_source"] == "offline_demo"


@pytest.mark.asyncio
async def test_pause_during_model_request_keeps_usage_but_not_fill(context):
    svc, _, _, model = service(context)
    entered, release = asyncio.Event(), asyncio.Event()
    decide = model.decide

    async def blocked(request):
        entered.set()
        await release.wait()
        return await decide(request)

    model.decide = blocked
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    task = asyncio.create_task(svc.step())
    await entered.wait()
    current = await svc.store.latest()
    await svc.pause(current.account_ref, current.revision)
    release.set()
    cycle = await task
    assert cycle.status == "discarded" and cycle.usage is not None
    assert (await svc.store.latest()).usdt == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["stale", "future", "drift", "confidence", "position", "funds", "loss", "model_error"],
)
async def test_hard_gates_never_fill(context, failure):
    svc, clock, market, model = service(context, choices=("BUY",))
    account = await svc.store.latest()
    if failure in ("funds", "position", "loss"):
        # Obtain a real first position, then exceed a limit on the second cycle.
        await svc.start(account.account_ref, account.revision)
        assert (await svc.step()).status == "filled"
        account = await svc.store.latest()
        if failure == "funds":
            market.price = Decimal("2000000")
        elif failure == "loss":
            market.price = Decimal("1")
        else:
            for _ in range(9):
                await svc.step()
    else:
        await svc.start(account.account_ref, account.revision)
    if failure in ("stale", "future"):
        market.age_seconds = 6 if failure == "stale" else -1
    if failure == "confidence":
        model.confidence = "0.4"
    if failure in ("drift", "model_error"):
        decide = model.decide

        async def changed(request):
            if failure == "model_error":
                raise RuntimeError("private provider diagnostic")
            result = await decide(request)
            market.price *= 2
            return result

        model.decide = changed
    before = await svc.store.latest()
    result = await svc.step()
    after = await svc.store.latest()
    assert result is None or result.status != "filled"
    assert (after.usdt, after.btc) == (before.usdt, before.btc)


@pytest.mark.asyncio
async def test_paper_controls_preserve_environment_on_advice_update(context):
    from agent_platform.application.agent_controls import AgentControlService
    from agent_platform.domain.agent_controls import AdviceSelection

    _, _, _, core, _, _, _ = context
    controls = AgentControlService(store=core, clock=FakeClock(NOW), production_reads=True)
    current = await controls.current()
    updated = await controls.update_advice(
        AdviceSelection(enabled=True, confirmed=True), expected_revision=current.revision
    )
    assert updated.operation.execution_environment == "paper"
    assert updated.trader_revision == current.trader_revision


@pytest.mark.asyncio
async def test_invalid_quote_does_not_claim_a_wait_decision(context):
    svc, _, market, model = service(context, choices=("WAIT",))
    decide = model.decide

    async def invalid_after(request):
        reply = await decide(request)
        market.age_seconds = 6
        return reply

    model.decide = invalid_after
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    cycle = await svc.step()
    assert cycle.status != "wait"


@pytest.mark.asyncio
async def test_switch_off_pauses_the_loop_before_next_model_request(context):
    from agent_platform.application.agent_controls import AgentControlService
    from agent_platform.domain.agent_controls import TraderSelection

    svc, clock, _, model = service(context)
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    controls = AgentControlService(store=context[3], clock=clock)
    current = await controls.current()
    await controls.update_trader(
        TraderSelection(mode="auto", execution_environment="paper", enabled=False, confirmed=True),
        expected_revision=current.revision,
    )
    assert await svc.step() is None
    assert not model.calls
    assert (await svc.store.latest()).status == "paused"


@pytest.mark.asyncio
async def test_old_wallet_sources_are_not_relabelled_by_new_process(context):
    svc, _, _, _ = service(context)
    svc.market_source = "binance_public"
    view = await svc.public_view()
    assert view["market_source"] == "offline_demo"
    assert view["source_compatible"] is False


@pytest.mark.asyncio
async def test_futures_view_cannot_display_or_value_old_spot_wallet(context):
    from agent_platform.application.sessions import SessionService
    from agent_platform.domain.session_market import SessionAnalysisTarget
    from agent_platform.domain.sessions import TradingStyle

    svc, clock, market, _ = service(context)
    original = await svc.store.latest()
    sessions = SessionService(svc.sessions, clock)
    await sessions.transition("session-1", "closed", 1)
    await sessions.create(
        TradingStyle(strength=80),
        SessionAnalysisTarget(market="usdt_perpetual", symbol="ETHUSDT"),
    )

    async def forbid_spot_quote():
        pytest.fail("A futures view must not value a Spot wallet")

    market.sample = forbid_spot_quote
    view = await svc.public_view()
    assert view["account"] is None and view["cycles"] == [] and view["equity_usdt"] is None
    assert view["market_compatible"] is False and view["paper_market"] == "spot"
    assert (await svc.store.get(original.account_ref)).account_ref == original.account_ref


@pytest.mark.asyncio
async def test_model_receives_bounded_closed_candles_and_features(context):
    import json

    from agent_platform.domain.market import Candle, FeatureSnapshot

    svc, _, market, model = service(context)
    original_sample, decide = market.sample, model.decide
    candle = Candle(
        symbol="BTCUSDT",
        opened_at=NOW - timedelta(minutes=2),
        closed_at=NOW - timedelta(minutes=1),
        open="60000",
        high="60010",
        low="59990",
        close="60001",
        volume="1",
    )

    async def sample():
        current = await original_sample()
        return type(current).model_validate(current.model_dump() | {"candles": (candle,)})

    async def features():
        return FeatureSnapshot(
            symbol="BTCUSDT",
            as_of=NOW,
            snapshot_id="features-test",
            warmup_ready=True,
            interval_return="0.001",
            ema_fast="60000",
            ema_slow="59999",
            atr="10",
            vwap="60000",
            volatility="0.01",
            volume_change="0",
            spread="1",
        )

    seen = []

    async def record(request):
        seen.append(json.loads(request.state_json))
        return await decide(request)

    market.sample, market.features, model.decide = sample, features, record
    account = await svc.store.latest()
    await svc.start(account.account_ref, account.revision)
    await svc.step()
    assert seen[0]["candles"][0]["close"] == "60001"
    assert seen[0]["features"]["ema_fast"] == "60000"
