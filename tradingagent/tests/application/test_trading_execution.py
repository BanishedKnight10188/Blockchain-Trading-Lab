"""Real journal plus replaceable execution: uncertainty never resubmits an order."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.futures_market import FuturesMarketSnapshot
from agent_platform.domain.trading_execution import ExecutionReceipt, TradingAccountSnapshot
from agent_platform.ports.persistence import EventIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from agent_platform.ports.trading_execution import ExecutionBlocked
from tests.adapters.test_futures_execution_backend import backend, cmd
from tests.adapters.test_futures_paper_store import context, running
from tests.domain.test_futures_paper import NOW, quote


class Market:
    def __init__(self, clock):
        self.clock, self.age, self.symbol = clock, 0, "ETHUSDT"
        self.source = "offline_replay"

    async def snapshot(self, symbol):
        q = quote(at=self.clock.utcnow() - timedelta(seconds=self.age))
        q = type(q).model_validate(q.model_dump() | {"symbol": self.symbol, "source": self.source})
        return FuturesMarketSnapshot(
            quote=q,
            index_price="2000",
            displayed_funding_rate="0",
            next_funding_at=NOW + timedelta(hours=8),
            bid_quantity="10",
            ask_quantity="10",
        )


class DeferredBackend:
    """A remote-like boundary double; persistence and application remain real."""

    def __init__(self, accounts):
        self.accounts = accounts
        self.submissions = []
        self.result = None
        self.failure = None
        self.gate = None

    async def account(self, scope, at):
        return await self.accounts.account(scope, at)

    async def submit(self, command, quote, at):
        self.submissions.append(command.command_id)
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        return self.result or ExecutionReceipt(
            command=command, status="accepted", backend_order_id="remote-1", observed_at=at
        )

    async def lookup(self, command, at):
        return self.result


async def setup(tmp_path, *, deferred=False):
    ctx = await context(tmp_path)
    a = await running(ctx)
    paper = backend(ctx[0])
    execution = DeferredBackend(paper) if deferred else paper
    clock = FakeClock(NOW)
    market = Market(clock)
    m = importlib.import_module("agent_platform.adapters.sqlite.trading_execution")
    journal = m.SqliteExecutionJournal(ctx[5])
    await journal.initialize()
    s = importlib.import_module("agent_platform.application.trading_execution")

    # This test boundary explicitly grants the pre-existing legacy wallet only.
    class LegacyTestAuthorizer:
        async def authorize(self, command, snapshot, account, at):
            if command.scope.account_ref != a.account_ref or command.decision_evidence is not None:
                from agent_platform.ports.trading_execution import ExecutionRejected

                raise ExecutionRejected("legacy_test_scope")

    service = s.TradeExecutionService(
        journal=journal,
        execution=execution,
        accounts=execution,
        market=market,
        clock=clock,
        market_source="offline_replay",
        authorizer=LegacyTestAuthorizer(),
    )
    return ctx, a, execution, clock, market, journal, service


@pytest.mark.asyncio
async def test_real_paper_uses_generic_service_and_persistent_receipt(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path)
    c = cmd(a)
    r = await service.submit(c)
    assert r.receipt.status == "filled" and r.quote.mark == 2000
    assert r.receipt.fee_usdt == 1 and (await ctx[0].get(a.account_ref)).free_usdt == 599
    assert await journal.get(c.command_id) == r
    clock.advance_to(NOW + timedelta(seconds=40))
    assert await service.submit(c) == r
    assert len(await ctx[0].recent(a.account_ref)) == 3


@pytest.mark.asyncio
async def test_same_application_handles_accepted_partial_and_filled_remote_receipts(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    accepted = await service.submit(c)
    assert accepted.receipt.status == "accepted" and (await ctx[0].get(a.account_ref)).quantity == 0
    clock.advance_to(NOW + timedelta(seconds=1))
    b.result = ExecutionReceipt(
        command=c,
        status="partially_filled",
        filled_quantity="0.4",
        average_price="2000",
        fee_usdt="0.4",
        backend_order_id="remote-1",
        backend_at=clock.utcnow(),
        observed_at=clock.utcnow(),
    )
    partial = await service.reconcile(c.command_id)
    assert partial.receipt.filled_quantity == Decimal("0.4")
    clock.advance_to(NOW + timedelta(seconds=2))
    b.result = ExecutionReceipt(
        command=c,
        status="filled",
        filled_quantity="1",
        average_price="2001",
        fee_usdt="1",
        backend_order_id="remote-1",
        backend_at=clock.utcnow(),
        observed_at=clock.utcnow(),
    )
    done = await service.reconcile(c.command_id)
    assert done.receipt.status == "filled" and done.receipt.average_price == 2001
    assert b.submissions == [c.command_id]


@pytest.mark.asyncio
async def test_submit_timeout_then_restart_queries_without_resubmission(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    b.failure = TimeoutError("private provider details")
    r = await service.submit(c)
    assert r.receipt.status == "unknown" and r.receipt.reason == "execution_unknown"
    assert "private" not in r.model_dump_json()
    with pytest.raises(ExecutionBlocked):
        await service.submit(cmd(a, command_id="second"))
    clock.advance_to(NOW + timedelta(seconds=40))
    b.result = ExecutionReceipt(
        command=c,
        status="filled",
        filled_quantity="1",
        average_price="2000",
        fee_usdt="1",
        backend_order_id="remote-1",
        backend_at=clock.utcnow(),
        observed_at=clock.utcnow(),
    )
    reopened = type(service)(
        journal=type(journal)(ctx[5]),
        execution=b,
        accounts=b,
        market=market,
        clock=clock,
        market_source="offline_replay",
    )
    assert (await reopened.submit(c)).receipt.status == "filled"
    assert b.submissions == [c.command_id]


@pytest.mark.asyncio
async def test_missing_lookup_does_not_authorize_retry_or_another_order(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    await service.submit(c)
    r = await service.reconcile(c.command_id)
    assert r.receipt.status == "unknown" and r.receipt.reason == "not_found"
    assert (await service.submit(c)).receipt.status == "unknown"
    with pytest.raises(ExecutionBlocked):
        await service.submit(cmd(a, command_id="new"))
    assert b.submissions == [c.command_id]


@pytest.mark.asyncio
async def test_cancel_after_submission_leaves_a_durable_unknown_command(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    b.gate = asyncio.Event()
    task = asyncio.create_task(service.submit(c))
    for _ in range(200):
        if b.submissions:
            break
        await asyncio.sleep(0.01)
    assert b.submissions == [c.command_id]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await journal.get(c.command_id)).receipt.status == "unknown"
    assert (await service.submit(c)).receipt.status == "unknown"
    assert b.submissions == [c.command_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stale", "wrong_symbol", "revision"])
async def test_preflight_rejects_before_execution_and_persists_reason(tmp_path, kind):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    if kind == "stale":
        market.age = 6
    elif kind == "wrong_symbol":
        market.symbol = "BTCUSDT"
    else:
        c = type(c).model_validate(c.model_dump() | {"expected_account_revision": 1})
    r = await service.submit(c)
    assert r.receipt.status == "rejected" and r.receipt.filled_quantity == 0
    assert b.submissions == [] and (await ctx[0].get(a.account_ref)).free_usdt == 1000


@pytest.mark.asyncio
async def test_committed_paper_fill_survives_journal_writeback_failure(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path)
    c = cmd(a)
    with sqlite3.connect(ctx[5]) as db:
        db.execute(
            "CREATE TRIGGER fail_filled BEFORE UPDATE ON execution_commands "
            "WHEN NEW.status='filled' BEGIN SELECT RAISE(ABORT, 'test writeback failure'); END"
        )
    with pytest.raises(PersistenceUnavailable):
        await service.submit(c)
    assert (await ctx[0].get(a.account_ref)).quantity == 1
    assert (await journal.get(c.command_id)).receipt.status == "pending"
    with sqlite3.connect(ctx[5]) as db:
        db.execute("DROP TRIGGER fail_filled")
    assert (await service.submit(c)).receipt.status == "filled"
    assert (await ctx[0].get(a.account_ref)).quantity == 1
    assert len(await ctx[0].recent(a.account_ref)) == 3


@pytest.mark.asyncio
async def test_two_journal_instances_atomically_claim_only_one_account_command(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path)
    other = type(journal)(ctx[5])
    results = await asyncio.gather(
        journal.reserve(cmd(a, command_id="one"), NOW),
        other.reserve(cmd(a, command_id="two"), NOW),
        return_exceptions=True,
    )
    assert sum(isinstance(x, ExecutionBlocked) for x in results) == 1
    with sqlite3.connect(ctx[5]) as db:
        assert db.execute("SELECT count(*) FROM execution_commands").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_same_id_different_command_cannot_reuse_or_modify_execution(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path)
    c = cmd(a)
    first = await service.submit(c)
    with pytest.raises(EventIdentityConflict):
        await service.submit(type(c).model_validate(c.model_dump() | {"quantity": "0.5"}))
    assert (
        await journal.get(c.command_id) == first and (await ctx[0].get(a.account_ref)).quantity == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["quantity", "time", "order_id", "price", "fee"])
async def test_journal_rejects_regressive_or_conflicting_partial_facts(tmp_path, mutation):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    await service.submit(c)
    partial = ExecutionReceipt(
        command=c,
        status="partially_filled",
        filled_quantity="0.4",
        average_price="2000",
        fee_usdt="0.4",
        backend_order_id="remote-1",
        backend_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=1),
    )
    current = await journal.get(c.command_id)
    saved = await journal.save(partial, current.revision)
    changes = {
        "quantity": {"filled_quantity": "0.2"},
        "time": {"observed_at": NOW},
        "order_id": {"backend_order_id": "another"},
        "price": {"average_price": "2001"},
        "fee": {"fee_usdt": "0.1"},
    }[mutation]
    with pytest.raises(ValueError):
        await journal.save(
            ExecutionReceipt.model_validate(partial.model_dump() | changes), saved.revision
        )
    assert await journal.get(c.command_id) == saved


@pytest.mark.asyncio
async def test_terminal_result_cannot_be_rewritten_and_duplicate_is_idempotent(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path)
    r = await service.submit(cmd(a))
    assert await journal.save(r.receipt, 1) == r
    changed = ExecutionReceipt.model_validate(r.receipt.model_dump() | {"average_price": "2001"})
    with pytest.raises(ValueError):
        await journal.save(changed, r.revision)
    assert await journal.get(r.command.command_id) == r


@pytest.mark.asyncio
async def test_journal_audit_failure_rolls_back_command_claim(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path)
    with sqlite3.connect(ctx[5]) as db:
        db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON execution_updates "
            "BEGIN SELECT RAISE(ABORT, 'test audit failure'); END"
        )
    with pytest.raises(PersistenceUnavailable):
        await journal.reserve(cmd(a), NOW)
    with pytest.raises(LookupError):
        await journal.get("order-1")


@pytest.mark.asyncio
async def test_journal_cas_rejects_stale_nonterminal_update(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    r = await service.submit(c)
    update = ExecutionReceipt(
        command=c, status="unknown", backend_order_id="remote-1", observed_at=NOW
    )
    with pytest.raises(RevisionConflict):
        await journal.save(update, 1)
    assert await journal.get(c.command_id) == r


@pytest.mark.asyncio
async def test_lost_reply_after_real_commit_recovers_original_fill_event(tmp_path):
    ctx, a, paper, clock, market, journal, service = await setup(tmp_path)

    class LostReply:
        async def submit(self, command, q, at):
            await paper.submit(command, q, at)
            clock.advance_to(NOW + timedelta(seconds=1))
            raise TimeoutError("lost after commit")

        async def lookup(self, command, at):
            return await paper.lookup(command, at)

    service.execution = LostReply()
    c = cmd(a)
    assert (await service.submit(c)).receipt.status == "unknown"
    clock.advance_to(NOW + timedelta(seconds=2))
    reopened = type(service)(
        journal=type(journal)(ctx[5]),
        execution=paper,
        accounts=paper,
        market=market,
        clock=clock,
        market_source="offline_replay",
    )
    recovered = await reopened.reconcile(c.command_id)
    assert recovered.receipt.status == "filled"
    assert recovered.receipt.backend_at == NOW and recovered.receipt.observed_at == clock.utcnow()
    assert (await ctx[0].get(a.account_ref)).quantity == 1


@pytest.mark.asyncio
async def test_first_future_quote_cannot_be_laundered_by_account_wait(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    market.age = -1

    class WaitAccounts:
        async def account(self, scope, at):
            clock.advance_to(NOW + timedelta(seconds=1))
            return await b.account(scope, clock.utcnow())

    service.accounts = WaitAccounts()
    assert (await service.submit(cmd(a))).receipt.status == "rejected"
    assert b.submissions == []


@pytest.mark.asyncio
async def test_mixed_account_quote_and_execution_quote_sources_are_rejected(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)

    class MixedAccounts:
        async def account(self, scope, at):
            original = await b.account(scope, at)
            q = type(quote()).model_validate(
                quote().model_dump() | {"source": "binance_futures_public"}
            )
            return TradingAccountSnapshot.model_validate(
                original.model_dump()
                | {"quote": q, "equity_usdt": "1000", "unrealized_pnl_usdt": "0"}
            )

    service.accounts = MixedAccounts()
    assert (await service.submit(cmd(a))).receipt.status == "rejected"
    assert b.submissions == []


@pytest.mark.asyncio
async def test_lookup_keeps_native_event_watermark_even_with_new_observation_time(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    c = cmd(a)
    await service.submit(c)
    r = await journal.get(c.command_id)
    current = ExecutionReceipt(
        command=c,
        status="partially_filled",
        filled_quantity="0.4",
        average_price="2000",
        fee_usdt="0.4",
        backend_order_id="remote-1",
        backend_at=NOW + timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=2),
    )
    saved = await journal.save(current, r.revision)
    older = ExecutionReceipt.model_validate(
        current.model_dump() | {"backend_at": NOW, "observed_at": NOW + timedelta(seconds=3)}
    )
    with pytest.raises(EventIdentityConflict):
        await journal.save(older, saved.revision)
    assert await journal.get(c.command_id) == saved


@pytest.mark.asyncio
async def test_unvalued_account_cannot_allow_an_unconfigured_market_source(tmp_path):
    ctx, a, b, clock, market, journal, service = await setup(tmp_path, deferred=True)
    market.source = "binance_futures_public"
    assert (await service.submit(cmd(a))).receipt.status == "rejected"
    assert b.submissions == []
