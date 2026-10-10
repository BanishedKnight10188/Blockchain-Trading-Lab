"""Real SQLite futures funds, current permissions, idempotency and recovery."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.adapters.sqlite.store import open_store
from agent_platform.domain.agent_controls import AgentControlState
from agent_platform.domain.events import SessionJournalEvent
from agent_platform.domain.model_modules import JevModuleSettings, JevTraderSettings
from agent_platform.domain.operating_modes import OperatingSettings
from agent_platform.domain.session_market import SessionAnalysisTarget
from agent_platform.domain.sessions import AgentSession, TradingStyle
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.adapters.test_paper_store import write_controls
from tests.domain.test_futures_paper import (
    NOW,
    funding,
    modules,
    order,
    quote,
    rules_data,
    settings_data,
)


async def context(tmp_path, *, market="usdt_perpetual"):
    module = importlib.import_module("agent_platform.adapters.sqlite.futures_paper")
    d, _ = modules()
    path = tmp_path / "futures.sqlite3"
    core = await open_store(path)
    sessions = SqliteSessionStore(path)
    session = AgentSession(
        session_id="s1",
        style=TradingStyle(strength=80),
        analysis_target=SessionAnalysisTarget(
            market=market, symbol="ETHUSDT" if market != "spot" else "BTCUSDT"
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    await sessions.create(
        session,
        SessionJournalEvent(event_id="created", kind="created", session=session, occurred_at=NOW),
    )
    session = session.transition("running", NOW)
    await sessions.save(
        session,
        1,
        SessionJournalEvent(
            event_id="running", kind="state_changed", session=session, occurred_at=NOW
        ),
    )
    controls = AgentControlState(
        revision=1,
        updated_at=NOW,
        operation=OperatingSettings(mode="auto", execution_environment="paper"),
        jev=JevModuleSettings(enabled=False),
        trader=JevTraderSettings(enabled=True),
    )
    await write_controls(core, controls)
    store = module.SqliteFuturesPaperStore(path)
    await store.initialize()
    return store, core, sessions, session, controls, path, d


async def running(ctx):
    store, _, _, session, _, _, d = ctx
    account = await store.create(
        session.session_id,
        d.FuturesPaperSettings(**settings_data()),
        d.FuturesPaperRules(**rules_data()),
        NOW,
        expected_style_revision=1,
    )
    return await store.start(
        account.account_ref,
        account.revision,
        NOW,
        expected_style_revision=1,
        expected_trader_revision=1,
    )


async def execute(store, account, command_id="open-1", *, at=NOW):
    return await store.execute(
        account.account_ref,
        account.revision,
        order(),
        quote(at=at),
        at,
        command_id=command_id,
        expected_style_revision=1,
        expected_trader_revision=1,
    )


@pytest.mark.asyncio
async def test_real_wallet_and_same_id_retry_do_not_double_spend(tmp_path):
    ctx = await context(tmp_path)
    store = ctx[0]
    account = await running(ctx)
    first = await execute(store, account)
    assert first.state.free_usdt == 599 and first.state.margin_usdt == 400
    assert await execute(store, account) == first
    assert await store.get(account.account_ref) == first.state
    history = await store.recent(account.account_ref)
    assert len(history) == 3 and history[0].command_id == "open-1"
    with pytest.raises(EventIdentityConflict):
        await store.execute(
            account.account_ref,
            account.revision,
            order(quantity="0.5"),
            quote(),
            NOW,
            command_id="open-1",
            expected_style_revision=1,
            expected_trader_revision=1,
        )


@pytest.mark.asyncio
async def test_two_instances_cannot_spend_same_revision_twice(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    second = type(ctx[0])(ctx[5])
    results = await asyncio.gather(
        execute(ctx[0], account, "one"), execute(second, account, "two"), return_exceptions=True
    )
    assert sum(isinstance(r, RevisionConflict) for r in results) == 1
    assert (await ctx[0].get(account.account_ref)).quantity == 1


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_wallet_and_operation(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    with sqlite3.connect(ctx[5]) as db:
        db.execute(
            "CREATE TRIGGER deny_futures_op BEFORE INSERT ON futures_paper_operations "
            "BEGIN SELECT RAISE(ABORT, 'test unavailable'); END"
        )
    with pytest.raises(PersistenceUnavailable):
        await execute(ctx[0], account)
    assert await ctx[0].get(account.account_ref) == account
    assert len(await ctx[0].recent(account.account_ref)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "closed", "style", "trader", "testnet"])
async def test_current_guard_blocks_late_work_without_spending(tmp_path, change):
    ctx = await context(tmp_path)
    store, core, sessions, session, controls, _, _ = ctx
    account = await running(ctx)
    at = NOW + timedelta(seconds=1)
    if change in ("pause", "closed", "style"):
        updated = (
            session.change_style(TradingStyle(strength=30), at)
            if change == "style"
            else session.transition("paused" if change == "pause" else "closed", at)
        )
        await sessions.save(
            updated,
            session.revision,
            SessionJournalEvent(
                event_id="changed",
                kind="style_changed" if change == "style" else "state_changed",
                session=updated,
                occurred_at=at,
            ),
        )
    else:
        updates = (
            {"trader": JevTraderSettings(enabled=False)}
            if change == "trader"
            else {"operation": OperatingSettings(mode="auto", execution_environment="testnet")}
        )
        changed = AgentControlState.model_validate(
            controls.model_dump()
            | updates
            | {"revision": 2, "trader_revision": 2, "updated_at": at}
        )
        await write_controls(core, changed)
    with pytest.raises(ValueError):
        await execute(store, account, at=at)
    assert await store.get(account.account_ref) == account
    assert len(await store.recent(account.account_ref)) == 2


@pytest.mark.asyncio
async def test_spot_session_cannot_create_a_futures_wallet(tmp_path):
    store, _, _, session, _, _, d = await context(tmp_path, market="spot")
    with pytest.raises(ValueError):
        await store.create(
            session.session_id,
            d.FuturesPaperSettings(**settings_data()),
            d.FuturesPaperRules(**rules_data()),
            NOW,
            expected_style_revision=1,
        )
    with pytest.raises(LookupError):
        await store.get("paper:futures:s1")


@pytest.mark.asyncio
async def test_recovery_keeps_funds_and_audit_and_requires_explicit_restart(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    first = await execute(ctx[0], account)
    reopened = type(ctx[0])(ctx[5])
    at = NOW + timedelta(seconds=1)
    await reopened.recover(at)
    recovered = await reopened.get(account.account_ref)
    assert recovered.status == "paused" and recovered.quantity == 1
    assert (
        recovered.free_usdt == first.state.free_usdt
        and recovered.margin_usdt == first.state.margin_usdt
    )
    with pytest.raises(ValueError):
        await execute(reopened, recovered, "no-restart", at=at)
    assert len(await reopened.recent(account.account_ref)) == 4
    await reopened.recover(at)
    assert len(await reopened.recent(account.account_ref)) == 4


@pytest.mark.asyncio
async def test_paused_funding_settlement_is_idempotent_and_does_not_wait_for_model(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    opened = await execute(ctx[0], account)
    paused = await ctx[0].pause(account.account_ref, opened.state.revision, NOW)
    f = funding()
    settled = await ctx[0].funding(
        account.account_ref, paused.revision, f, f.settled_at, command_id="funding-1"
    )
    assert settled.state.margin_usdt == 398 and settled.state.status == "paused"
    assert (
        await ctx[0].funding(
            account.account_ref, paused.revision, f, f.settled_at, command_id="funding-1"
        )
        == settled
    )
    assert (await ctx[0].get(account.account_ref)).free_usdt == 599


@pytest.mark.asyncio
async def test_reconfigure_cannot_reset_spent_money_or_raise_policy(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    spent = await execute(ctx[0], account)
    same = await ctx[0].create(
        "s1", account.settings, account.rules, NOW, expected_style_revision=1
    )
    assert same == spent.state
    changed = ctx[6].FuturesPaperSettings(**(settings_data() | {"initial_usdt": "2000"}))
    with pytest.raises(ValueError):
        await ctx[0].create("s1", changed, account.rules, NOW, expected_style_revision=1)


@pytest.mark.asyncio
async def test_saved_operations_include_replayable_quote_order_and_funding(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    opened = await execute(ctx[0], account)
    record = (await ctx[0].recent(account.account_ref))[0]
    assert record.quote == quote() and record.order == order()
    assert record.quote.source == "offline_replay"
    f = funding()
    await ctx[0].funding(
        account.account_ref, opened.state.revision, f, f.settled_at, command_id="funding-1"
    )
    assert (await ctx[0].recent(account.account_ref))[0].funding == f


@pytest.mark.asyncio
async def test_paused_disabled_trader_still_liquidates_on_fresh_mark_with_stale_book(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    opened = await execute(ctx[0], account)
    paused = await ctx[0].pause(account.account_ref, opened.state.revision, NOW)
    c = AgentControlState.model_validate(
        ctx[4].model_dump()
        | {"revision": 2, "trader_revision": 2, "trader": JevTraderSettings(enabled=False)}
    )
    await write_controls(ctx[1], c)
    at = NOW + timedelta(seconds=10)
    d, _ = modules()
    q = d.FuturesPaperQuote.model_validate(quote("1400", at=at).model_dump() | {"book_at": NOW})
    result = await ctx[0].mark(account.account_ref, paused.revision, q, at, command_id="risk-1")
    assert result.state.status == "liquidated" and result.state.free_usdt == 599
    assert result.operation.shortfall_usdt == 200
    assert (
        await ctx[0].mark(account.account_ref, paused.revision, q, at, command_id="risk-1")
        == result
    )


@pytest.mark.asyncio
async def test_funding_new_id_cannot_repeat_same_settlement(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    opened = await execute(ctx[0], account)
    f = funding()
    once = await ctx[0].funding(
        account.account_ref, opened.state.revision, f, f.settled_at, command_id="one"
    )
    with pytest.raises(ValueError):
        await ctx[0].funding(
            account.account_ref, once.state.revision, f, f.settled_at, command_id="two"
        )
    assert await ctx[0].get(account.account_ref) == once.state


@pytest.mark.asyncio
async def test_mark_only_cannot_replace_consumed_book_at_same_timestamp(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    opened = await execute(ctx[0], account)
    at = NOW + timedelta(seconds=1)
    d, _ = modules()
    conflict = d.FuturesPaperQuote.model_validate(
        quote("2100", at=at).model_dump() | {"book_at": NOW, "bid": "2200", "ask": "2200"}
    )
    assert (
        await ctx[0].mark(
            account.account_ref, opened.state.revision, conflict, at, command_id="mark"
        )
        is None
    )
    with pytest.raises(ValueError):
        await ctx[0].execute(
            account.account_ref,
            opened.state.revision,
            order("reduce"),
            conflict,
            at + timedelta(seconds=1),
            command_id="close",
            expected_style_revision=1,
            expected_trader_revision=1,
        )
    restored = await ctx[0].get(account.account_ref)
    assert restored.free_usdt == 599 and restored.quantity == 1
    assert restored.last_quote.mark == 2100 and restored.last_quote.bid == 2000


@pytest.mark.asyncio
async def test_recovery_does_not_restore_an_older_quote_after_a_safe_mark(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    opened = await execute(ctx[0], account)
    at = NOW + timedelta(seconds=1)
    q = quote("2100", at=at)
    assert (
        await ctx[0].mark(account.account_ref, opened.state.revision, q, at, command_id="safe")
        is None
    )
    reopened = type(ctx[0])(ctx[5])
    await reopened.recover(at + timedelta(seconds=1))
    restored = await reopened.get(account.account_ref)
    assert restored.status == "paused" and restored.last_quote == q


@pytest.mark.asyncio
async def test_safe_mark_watermark_survives_restart_and_blocks_older_trade_quote(tmp_path):
    ctx = await context(tmp_path)
    account = await running(ctx)
    at = NOW + timedelta(seconds=1)
    q = quote("2100", at=at)
    assert (
        await ctx[0].mark(account.account_ref, account.revision, q, at, command_id="safe-mark")
        is None
    )
    reopened = type(ctx[0])(ctx[5])
    with pytest.raises(ValueError):
        await reopened.execute(
            account.account_ref,
            account.revision,
            order(),
            quote("2000"),
            at + timedelta(seconds=1),
            command_id="old",
            expected_style_revision=1,
            expected_trader_revision=1,
        )
    assert (await reopened.get(account.account_ref)).free_usdt == 1000
    assert (await reopened.get(account.account_ref)).revision == account.revision
    assert (
        await reopened.mark(account.account_ref, account.revision, q, at, command_id="safe-mark")
        is None
    )
    with pytest.raises(EventIdentityConflict):
        await reopened.mark(
            account.account_ref, account.revision, quote("2101", at=at), at, command_id="safe-mark"
        )
