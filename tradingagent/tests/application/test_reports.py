"""Human reports are pending local evidence until an exact exchange ID confirms them."""

import importlib
import sqlite3
from datetime import timedelta

import pytest

from agent_platform.ports.persistence import RequestIdentityConflict
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_attribution import setup as imported_setup
from tests.domain.test_decisions import NOW

context = _context


def report(identity="report-1", trade_id="trade-1", **updates):
    domain = importlib.import_module("agent_platform.domain.reports")
    data = dict(
        report_id=identity,
        account_ref="local-spot",
        side="buy",
        price="60000",
        quantity="0.01",
        executed_at=NOW,
        reported_at=NOW,
        exchange_trade_id=trade_id,
    )
    data.update(updates)
    return domain.UserReportedTrade(**data)


async def service(context, *, imported=True):
    value, _, trade = await imported_setup(context)
    if not imported:
        with sqlite3.connect(context[0]) as connection:
            connection.execute("DELETE FROM trade_quote_evidence")
            connection.execute("DELETE FROM observed_trades")
    adapter = importlib.import_module("agent_platform.adapters.sqlite.reports")
    application = importlib.import_module("agent_platform.application.reports")
    store = adapter.SqliteReportStore(context[0], clock=value.clock)
    return (
        application.ReportService(store=store, clock=value.clock, account_ref="local-spot"),
        store,
        trade,
    )


@pytest.mark.asyncio
async def test_manual_report_never_creates_exchange_fill_or_changes_balance(context):
    value, _, _ = await service(context, imported=False)
    balance = await context[1].account_snapshot("local-spot")
    receipt = await value.record(report(trade_id=None))
    assert receipt.state.report.source == "user_reported"
    assert receipt.state.status == "pending" and receipt.revision == 1
    assert not await context[1].observed_trades("local-spot", "BTCUSDT")
    assert await context[1].account_snapshot("local-spot") == balance


@pytest.mark.asyncio
async def test_explicit_id_verification_preserves_report_and_original_exchange_fact(context):
    value, store, trade = await service(context)
    original = report()
    await value.record(original)
    receipt = await value.verify("report-1", "verify-1", 1)
    assert receipt.state.status == "verified" and receipt.revision == 2
    assert receipt.state.matched_trade_id == "trade-1"
    assert receipt.state.report == original and receipt.state.report.source == "user_reported"
    assert await context[1].observed_trades("local-spot", "BTCUSDT") == (trade,)
    assert (await store.report("report-1", account_ref="local-spot")).state == receipt.state
    assert (await store.report("report-1", account_ref="local-spot")).event_id == receipt.event_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trade_id,reason", [(None, "exchange_id_missing"), ("absent", "exchange_trade_not_imported")]
)
async def test_missing_id_or_missing_fill_remains_pending_without_time_guess(
    context, trade_id, reason
):
    value, _, _ = await service(context)
    await value.record(report(trade_id=trade_id))
    receipt = await value.verify("report-1", "verify-1", 1)
    assert receipt.state.status == "pending" and receipt.state.matched_trade_id is None
    assert reason in receipt.state.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"price": "59999"}, "price_mismatch"),
        ({"quantity": "0.02"}, "quantity_mismatch"),
        ({"side": "sell"}, "side_mismatch"),
        ({"executed_at": NOW - timedelta(seconds=1)}, "execution_time_mismatch"),
    ],
)
async def test_conflicting_report_does_not_merge_or_rewrite_exchange_fact(context, updates, reason):
    value, _, trade = await service(context)
    await value.record(report(**updates))
    receipt = await value.verify("report-1", "verify-1", 1)
    assert receipt.state.status == "conflict" and receipt.state.matched_trade_id is None
    assert reason in receipt.state.reasons
    assert await context[1].observed_trades("local-spot", "BTCUSDT") == (trade,)


@pytest.mark.asyncio
async def test_report_and_verification_exact_retries_survive_restart(context):
    value, store, _ = await service(context)
    original = report()
    first = await value.record(original)
    verified = await value.verify("report-1", "verify-1", 1)
    value.clock.advance_to(NOW + timedelta(seconds=120))
    value.store = type(store)(context[0], clock=value.clock)
    assert await value.record(original) == first
    assert await value.verify("report-1", "verify-1", 1) == verified
    assert (await store.report("report-1", account_ref="local-spot")).revision == 2
    with pytest.raises(RequestIdentityConflict):
        await value.record(report(price="59999"))
    with pytest.raises(RequestIdentityConflict):
        await value.verify("report-1", "verify-1", 2)
    with pytest.raises(RevisionConflict):
        await value.verify("report-1", "verify-2", 1)


@pytest.mark.asyncio
async def test_verification_failure_rolls_back_revision_and_all_operation_audits(context):
    value, store, _ = await service(context)
    await value.record(report())
    with sqlite3.connect(context[0]) as connection:
        connection.execute("""CREATE TRIGGER reject_report_verification
            BEFORE INSERT ON journal_events
            WHEN NEW.kind='user_trade_verified'
            BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END;""")
    with pytest.raises(PersistenceUnavailable):
        await value.verify("report-1", "verify-1", 1)
    assert (await store.report("report-1", account_ref="local-spot")).revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["another", "paper:local"])
async def test_report_scope_cannot_cross_account_or_paper_namespace(context, scope):
    value, _, _ = await service(context)
    with pytest.raises(ValueError):
        await value.record(report(account_ref=scope))


@pytest.mark.asyncio
async def test_similar_reports_with_distinct_ids_remain_distinct_local_facts(context):
    value, store, _ = await service(context)
    await value.record(report("report-1", None))
    await value.record(report("report-2", None))
    first, second = [
        await store.report(identity, account_ref="local-spot")
        for identity in ("report-1", "report-2")
    ]
    assert first.state.report.report_id != second.state.report.report_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["verified", "conflict"])
async def test_current_report_projection_requires_committed_verification_proof(context, status):
    from agent_platform.domain.events import JournalEvent, StateRecord
    from agent_platform.domain.reports import ReportState

    value, store, _ = await service(context)
    original = report(trade_id="not-imported")
    await value.record(original)
    fake = ReportState(
        report=original,
        status=status,
        checked_at=NOW,
        matched_trade_id="not-imported" if status == "verified" else None,
        reasons=() if status == "verified" else ("price_mismatch",),
    )
    changed = StateRecord(
        key=original.aggregate_id, revision=2, state_type="user_report", state=fake, updated_at=NOW
    )
    await context[1].save(
        changed,
        1,
        JournalEvent(
            event_id="unproven-report-update",
            aggregate_id=changed.key,
            kind="state_changed",
            payload=changed,
            occurred_at=NOW,
        ),
    )
    with pytest.raises(ValueError):
        await store.report("report-1", account_ref="local-spot")


@pytest.mark.asyncio
async def test_old_pending_verification_retry_survives_later_exact_trade_import(context):
    from agent_platform.domain.account import TradeBatch
    from agent_platform.domain.events import JournalEvent

    value, _, trade = await service(context, imported=False)
    await value.record(report())
    pending = await value.verify("report-1", "verify-1", 1)
    batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT", trades=(trade,))
    await context[1].ingest(
        batch,
        context[-1],
        JournalEvent(
            event_id="later-import",
            aggregate_id="local-spot",
            kind="trades_imported",
            payload=batch,
            occurred_at=NOW,
        ),
    )
    verified = await value.verify("report-1", "verify-2", 2)
    assert verified.state.status == "verified"
    assert await value.verify("report-1", "verify-1", 1) == pending


@pytest.mark.asyncio
async def test_later_event_only_verification_cannot_shadow_committed_current_result(context):
    from agent_platform.domain.events import JournalEvent
    from agent_platform.domain.reports import ReportVerification

    value, store, trade = await service(context)
    await value.record(report())
    verified = await value.verify("report-1", "verify-1", 1)
    dangling = ReportVerification(
        operation_id="dangling-only-fact",
        expected_revision=1,
        state=verified.state,
        exchange_evidence=trade,
    )
    await context[1].append(
        JournalEvent(
            event_id=store.verification_identity(dangling.operation_id, "fact"),
            aggregate_id=dangling.operation_id,
            kind="user_trade_verified",
            payload=dangling,
            occurred_at=NOW,
        )
    )
    assert await store.report("report-1", account_ref="local-spot") == verified
    assert (await value.verify("report-1", "verify-2", 2)).revision == 3
