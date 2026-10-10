"""Paper backend translates real persistent funds into neutral execution facts."""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.domain.trading_execution import ExecutionScope, TradeCommand
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.trading_execution import ExecutionRejected
from tests.adapters.test_futures_paper_store import context, running
from tests.domain.test_futures_paper import NOW, quote
from tests.domain.test_trading_execution import command_data, scope_data


def backend(store):
    return importlib.import_module("agent_platform.adapters.paper.futures").PaperFuturesBackend(
        store, market_source="offline_replay"
    )


def cmd(account, **changes):
    return TradeCommand(**command_data(expected_account_revision=account.revision, **changes))


@pytest.mark.asyncio
@pytest.mark.parametrize("action,side", [("open_long", "long"), ("open_short", "short")])
async def test_backend_executes_neutral_command_and_reconciles_actual_paper_funds(
    tmp_path, action, side
):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    c = cmd(a, action=action)
    filled = await b.submit(c, quote(), NOW)
    assert filled.status == "filled" and filled.filled_quantity == 1
    assert filled.average_price == 2000 and filled.fee_usdt == 1
    current = await b.account(c.scope, NOW)
    assert current.free_usdt == 599 and current.margin_usdt == 400
    assert current.quantity == 1 and current.side == side
    assert current.equity_usdt == 999 and current.unrealized_pnl_usdt == 0


@pytest.mark.asyncio
async def test_backend_partial_reduce_preserves_hand_checked_cash_and_fees(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    await b.submit(cmd(a), quote(), NOW)
    at = NOW + timedelta(seconds=1)
    a = await ctx[0].get(a.account_ref)
    c = cmd(
        a,
        command_id="reduce-1",
        action="reduce",
        quantity="0.4",
        created_at=at,
        expires_at=at + timedelta(seconds=5),
    )
    r = await b.submit(c, quote("2100", at=at), at)
    current = await b.account(c.scope, at)
    assert r.status == "filled" and r.fee_usdt == Decimal("0.42")
    assert current.free_usdt == Decimal("798.58") and current.margin_usdt == 240
    assert current.realized_pnl_usdt == 40 and current.quantity == Decimal("0.6")
    assert current.equity_usdt == Decimal("1098.58")


@pytest.mark.asyncio
async def test_restart_and_expired_retry_query_original_fill_without_spending_again(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    c = cmd(a)
    r = await backend(ctx[0]).submit(c, quote(), NOW)
    reopened = type(ctx[0])(ctx[5])
    later = NOW + timedelta(seconds=40)
    again = await backend(reopened).submit(c, quote("2200", at=later), later)
    assert again.model_dump(exclude={"observed_at"}) == r.model_dump(exclude={"observed_at"})
    assert again.observed_at == later and again.backend_at == NOW
    assert (await reopened.get(a.account_ref)).quantity == 1
    assert len(await reopened.recent(a.account_ref)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        dict(quantity="0.5"),
        dict(action="open_short"),
        dict(style_revision=2),
        dict(trader_revision=2),
        dict(expected_account_revision=3),
    ],
)
async def test_lookup_same_id_with_different_order_or_guard_is_a_conflict(tmp_path, changes):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    await b.submit(cmd(a), quote(), NOW)
    changed = TradeCommand.model_validate(cmd(a).model_dump() | changes)
    with pytest.raises(EventIdentityConflict):
        await b.lookup(changed, NOW)
    assert (await ctx[0].get(a.account_ref)).free_usdt == 599


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [dict(environment="testnet", account_ref="testnet:futures:alice"), dict(symbol="BTCUSDT")],
)
async def test_backend_rejects_wrong_environment_or_contract_without_creating_trade(
    tmp_path, changes
):
    ctx = await context(tmp_path)
    a = await running(ctx)
    c = cmd(a, scope=scope_data(**changes))
    with pytest.raises(ExecutionRejected):
        await backend(ctx[0]).submit(c, quote(), NOW)
    assert (await ctx[0].get(a.account_ref)).free_usdt == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["expired", "future", "stale", "old_revision", "wrong_source", "unsafe_quote"]
)
async def test_preflight_rejection_keeps_virtual_cash_unchanged(tmp_path, kind):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    c, q, at = cmd(a), quote(), NOW
    if kind == "expired":
        at += timedelta(seconds=6)
        q = quote(at=at)
    elif kind == "future":
        c = cmd(a, created_at=NOW + timedelta(seconds=1), expires_at=NOW + timedelta(seconds=6))
    elif kind == "stale":
        c = cmd(a, expires_at=NOW + timedelta(seconds=20))
        at += timedelta(seconds=6)
    elif kind == "old_revision":
        c = TradeCommand.model_validate(c.model_dump() | {"expected_account_revision": 1})
    elif kind == "wrong_source":
        q = type(q).model_validate(q.model_dump() | {"source": "binance_futures_public"})
    else:
        q = q.model_copy(update={"mark": "0"})
    with pytest.raises(ExecutionRejected):
        await b.submit(c, q, at)
    assert (await ctx[0].get(a.account_ref)).free_usdt == 1000


@pytest.mark.asyncio
async def test_account_reads_cash_without_inventing_a_quote_and_stale_valuation(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    s = ExecutionScope(**scope_data())
    snap = await b.account(s, NOW)
    assert snap.free_usdt == 1000 and snap.equity_usdt is None and snap.quote is None
    await b.submit(cmd(a), quote(), NOW)
    stale = await b.account(s, NOW + timedelta(seconds=6))
    assert stale.free_usdt == 599 and stale.quote.mark == 2000 and stale.equity_usdt is None


@pytest.mark.asyncio
async def test_lookup_is_not_limited_to_recent_fifty_operations(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    c = cmd(a)
    r = await b.submit(c, quote(), NOW)
    for n in range(1, 55):
        at = NOW + timedelta(seconds=n)
        a = await ctx[0].get(a.account_ref)
        await ctx[0].mark(a.account_ref, a.revision, quote(at=at), at, command_id=f"mark-{n}")
    assert all(x.command_id != c.command_id for x in await ctx[0].recent(a.account_ref))
    restored = await b.lookup(c, NOW + timedelta(seconds=60))
    assert restored.model_dump(exclude={"observed_at"}) == r.model_dump(exclude={"observed_at"})


@pytest.mark.asyncio
async def test_lookup_unsubmitted_command_is_none(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    assert await backend(ctx[0]).lookup(cmd(a), NOW) is None


@pytest.mark.asyncio
async def test_full_command_lifetime_is_part_of_persistent_execution_identity(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    c = cmd(a)
    await b.submit(c, quote(), NOW)
    changed = TradeCommand.model_validate(
        c.model_dump()
        | {"created_at": NOW - timedelta(seconds=1), "expires_at": NOW + timedelta(seconds=4)}
    )
    with pytest.raises(EventIdentityConflict):
        await b.lookup(changed, NOW)
    assert (await ctx[0].get(a.account_ref)).quantity == 1


@pytest.mark.asyncio
async def test_liquidation_rejects_requested_open_and_is_recoverable_by_command(tmp_path):
    ctx = await context(tmp_path)
    a = await running(ctx)
    b = backend(ctx[0])
    await b.submit(cmd(a), quote(), NOW)
    current = await ctx[0].get(a.account_ref)
    at = NOW + timedelta(seconds=1)
    c = cmd(current, command_id="after-drop", created_at=at, expires_at=at + timedelta(seconds=5))
    result = await b.submit(c, quote("1500", at=at), at)
    final = await ctx[0].get(a.account_ref)
    assert final.status == "liquidated" and final.quantity == 0
    assert result.status == "rejected" and result.reason == "account_liquidated"
    assert result.filled_quantity == 0 and result.average_price is None
    recovered = await b.lookup(c, at + timedelta(seconds=1))
    assert recovered.status == "rejected" and recovered.backend_at == at
