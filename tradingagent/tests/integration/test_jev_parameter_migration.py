"""Upgrade a flat paused wallet without creating funds or losing old history."""

from datetime import timedelta

import pytest

from agent_platform.domain.trading_runtime import TradingLimits, TradingPolicy
from agent_platform.ports.futures_paper import FuturesPaperGuardConflict
from agent_platform.ports.sessions import RevisionConflict
from tests.domain.test_futures_paper import NOW, settings_data
from tests.integration import test_futures_trading_core as core

assembled = core.assembled


@pytest.mark.asyncio
async def test_dynamic_upgrade_preserves_funds_and_drives_ten_times_choice(assembled):
    from agent_platform.adapters.sqlite.futures_parameters import SqliteFuturesParameterMigration

    svc, clock, ctx, _ = assembled
    run = await svc.configure(
        TradingLimits(**settings_data()),
        TradingPolicy(
            order_notional_usdt="100",
            max_price_drift_bps="20",
            min_confidence="0.8",
            strategy_instructions="Explicit test rule.",
        ),
        session_id="s1",
        style_revision=1,
    )
    before = await svc.backend.store.get(run.scope.account_ref)
    migration = SqliteFuturesParameterMigration(ctx[5])
    after = await migration.enable(
        run.scope.account_ref,
        before.revision,
        NOW,
        style_revision=1,
        leverage_choices=(1, 2, 5, 10),
    )
    assert after.free_usdt == before.free_usdt and after.fees_usdt == before.fees_usdt
    assert after.quantity == 0 and after.status == "paused"
    assert after.settings.max_leverage == 10 and after.revision == before.revision + 1
    upgraded = await svc.store.run(run.scope.account_ref)
    assert upgraded.policy.decision_mode == "parameterized"
    assert upgraded.limits.max_position_notional == run.limits.max_position_notional
    records = await svc.backend.store.recent(run.scope.account_ref)
    assert any(r.kind == "configure" for r in records) and records[0].kind == "parameterize"
    with migration._connect() as db:
        prior, replacement = db.execute(
            "SELECT old_body,new_body FROM futures_policy_migrations WHERE account_ref=?",
            (run.scope.account_ref,),
        ).fetchone()
        assert '"decision_mode":"fixed_notional"' in prior
        assert '"decision_mode":"parameterized"' in replacement
    with pytest.raises(RevisionConflict):
        await migration.enable(
            run.scope.account_ref,
            before.revision,
            NOW,
            style_revision=1,
            leverage_choices=(1, 2, 5, 10),
        )
    repeated = await migration.enable(
        run.scope.account_ref,
        after.revision,
        NOW,
        style_revision=1,
        leverage_choices=(1, 2, 5, 10),
    )
    assert repeated == after and len(await migration.recent(run.scope.account_ref)) == len(records)
    # Reconfirming the upgraded configuration must not reset the wallet.
    await svc.configure(upgraded.limits, upgraded.policy, session_id="s1", style_revision=1)
    await svc.start(
        account_ref=run.scope.account_ref,
        expected_revision=after.revision,
        style_revision=1,
        trader_revision=1,
    )
    svc.model.choices = ("OPEN_LONG_M5_L10",)
    assert (await svc.step()).status == "filled"
    account = await svc.backend.account(run.scope, clock.utcnow())
    assert account.leverage == 10 and account.quantity > 0
    clock.advance_to(NOW + timedelta(seconds=1))
    await svc.pause(account_ref=run.scope.account_ref, expected_revision=account.revision)
    with pytest.raises(FuturesPaperGuardConflict):
        current = await svc.backend.store.get(run.scope.account_ref)
        await migration.enable(
            run.scope.account_ref,
            current.revision,
            clock.utcnow(),
            style_revision=1,
            leverage_choices=(1, 2, 5, 10),
        )
