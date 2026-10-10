"""Actual reopen preserves facts; persisted balances cannot substitute for fresh evidence."""

from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.sqlite.store import SqliteStore
from agent_platform.application.reviews import ReviewService
from agent_platform.application.sessions import SessionService
from agent_platform.bootstrap import build_application_services
from agent_platform.config import RuntimeConfig
from agent_platform.domain.costs import BudgetRequest, ModelUsage
from agent_platform.domain.sessions import TradingStyle
from agent_platform.runtime.review_jobs import ReviewJobService
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_attribution import setup
from tests.domain.test_decisions import NOW

context = _context


@pytest.mark.asyncio
async def test_restart_keeps_style_cursor_unknown_fees_jobs_but_no_fresh_account(
    context, monkeypatch
):
    path, store, sessions, running, _ = context
    await setup(context)
    clock = FakeClock(NOW)
    # This test verifies a persisted pending job; due execution is tested separately.
    # Use the same clock on reopen so the host date cannot execute the followup first.
    monkeypatch.setattr("agent_platform.bootstrap.SystemClock", lambda: clock)
    changed = await SessionService(sessions, clock).change_style(
        running.session_id, TradingStyle(strength=78), running.revision
    )
    cutoff = NOW
    review_store = SqliteStore(path, clock=clock)
    reviews = ReviewService(store=review_store, clock=clock, account_ref="local-spot")
    await reviews.create_group("recovery-group", cutoff)
    await reviews.generate("recovery-group", cutoff, "initial")
    jobs = await ReviewJobService(
        store=review_store, clock=clock, account_ref="local-spot"
    ).schedule_followups("recovery-group", (24,))
    requested = clock.utcnow()
    request = BudgetRequest(
        request_id="recovery-budget",
        route_id="fixture",
        purpose="advisory",
        price_version="fixture",
        estimated_cost_usd="0.3",
        daily_limit_usd="1",
        requested_at=requested,
    )
    reservation = await store.reserve(request)
    await store.settle(
        reservation.reservation_id,
        ModelUsage(
            request_id=request.request_id,
            route_id="fixture",
            model_version="fixture",
            input_tokens=0,
            output_tokens=0,
            token_counts_known=False,
            estimated_cost_usd="0.3",
            billing_status="unknown",
            recorded_at=requested,
        ),
    )
    cursor = await store.cursor("local-spot", "BTCUSDT")
    async with build_application_services(
        path, RuntimeConfig(account_ref="local-spot")
    ) as services:
        assert await services.sessions.store.active() == changed
        reopened = services.reviews.store
        assert await reopened.cursor("local-spot", "BTCUSDT") == cursor
        assert len(await reopened.observed_trades("local-spot", "BTCUSDT")) == 1
        budget = await reopened.budget_balance(requested, daily_limit_usd=Decimal("1"))
        assert budget.reserved_usd == Decimal("0.3") and budget.hourly_call_count == 1
        assert (await reopened.review_jobs("local-spot"))[0].job_id == jobs[0].job_id
        assert len(await reopened.review_versions("recovery-group", account_ref="local-spot")) == 1
        overview = await services.queries.overview()
        assert overview.account.status == "not_connected" and overview.account.quantity is None
        assert overview.advice.status != "published" and overview.advice.quantity is None
    assert not services.runtime.running
