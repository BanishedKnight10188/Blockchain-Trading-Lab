"""Zero funding settlements must never be presented as trades."""
from datetime import timedelta

import pytest

from agent_platform.domain.futures_values import FuturesFunding
from tests.domain.test_futures_paper import NOW
from tests.integration.test_futures_trade_archive import assembled as _assembled
from tests.integration.test_futures_trade_archive import configure

assembled = _assembled


@pytest.mark.asyncio
async def test_empty_wallet_funding_is_retained_but_trade_count_is_zero(assembled):
    svc, clock, _, _ = assembled
    run = await configure(svc)
    svc.model.choices = ("WAIT",)
    assert (await svc.step()).status == "wait"
    for index in range(6):
        at = NOW + timedelta(seconds=index + 1)
        clock.advance_to(at)
        await svc.backend.settle(
            run.scope,
            FuturesFunding(symbol=run.scope.symbol, source=svc.backend.market_source, rate="0.001",
                           mark="2000", settled_at=at),
            at,
        )
    summary = await svc.archive.summary(run.scope.account_ref)
    assert summary["counts"] == {"trade": 0, "funding": 6, "liquidation": 0}
    page = await svc.archive.page(run.scope.account_ref)
    assert summary["total"] == page.total == 6
    assert all(entry.record.operation.funding_usdt == 0 for entry in page.entries)
    assert all(entry.record.execution_command is None for entry in page.entries)


@pytest.mark.asyncio
async def test_archive_counts_distinguish_fill_funding_and_forced_liquidation(assembled):
    svc, clock, _, _ = assembled
    run = await configure(svc)
    await svc.backend.settle(
        run.scope,
        FuturesFunding(symbol=run.scope.symbol, source=svc.backend.market_source, rate="0.001",
                       mark="2000", settled_at=NOW),
        NOW,
    )
    svc.model.choices = ("OPEN_LONG_M20_L10",)
    clock.advance_to(NOW + timedelta(seconds=1))
    assert (await svc.step()).status == "filled"
    clock.advance_to(NOW + timedelta(seconds=2))
    await svc.backend.settle(
        run.scope,
        FuturesFunding(symbol=run.scope.symbol, source=svc.backend.market_source, rate="0.1",
                       mark="3000", settled_at=clock.utcnow()),
        clock.utcnow(),
    )
    summary = await svc.archive.summary(run.scope.account_ref)
    assert summary["counts"] == {"trade": 1, "funding": 1, "liquidation": 1}
    assert summary["total"] == 3
