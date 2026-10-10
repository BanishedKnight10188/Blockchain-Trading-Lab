"""Actual trades, attribution, account revisions and cursor are one durable transaction."""

import importlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from agent_platform.domain.account import AccountSnapshot, ObservedTrade, TradeBatch, TradeCursor
from agent_platform.domain.events import JournalEvent
from agent_platform.domain.reviews import TradeAttribution
from agent_platform.ports.persistence import (
    EventIdentityConflict,
    ObservationConflict,
    ObservationStorePort,
)
from agent_platform.ports.sessions import PersistenceUnavailable
from tests.domain.test_decisions import NOW


@pytest_asyncio.fixture
async def context(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite.observations")
    path = tmp_path / "agent.sqlite3"
    store = module.SqliteObservationStore(path)
    await store.initialize()
    return module, store, path


def trade(trade_id, offset=1, account_ref="account-1", symbol="BTCUSDT", price="60000"):
    return ObservedTrade(
        account_ref=account_ref,
        symbol=symbol,
        trade_id=trade_id,
        order_id=trade_id,
        side="buy",
        price=price,
        quantity="0.001",
        fee="0",
        fee_asset="USDT",
        executed_at=NOW + timedelta(seconds=offset),
    )


def batch(trades=None, account_ref="account-1", symbol="BTCUSDT", next_cursor=None):
    facts = (
        tuple(trades)
        if trades is not None
        else (trade("t1", 1, account_ref, symbol), trade("t2", 2, account_ref, symbol))
    )
    cursor = (
        next_cursor
        if next_cursor is not None
        else (
            TradeCursor(last_trade_id=facts[-1].trade_id, last_executed_at=facts[-1].executed_at)
            if facts
            else TradeCursor()
        )
    )
    return TradeBatch(account_ref=account_ref, symbol=symbol, trades=facts, next_cursor=cursor)


def account(account_ref="account-1", offset=5, quantity="0.01", revision=0):
    return AccountSnapshot(
        account_ref=account_ref,
        as_of=NOW + timedelta(seconds=offset),
        account_revision=revision,
        balances=({"asset": "BTC", "free": quantity, "locked": "0"},),
    )


def audit(facts, event_id="import-1", offset=5):
    return JournalEvent(
        event_id=event_id,
        aggregate_id=facts.account_ref,
        kind="trades_imported",
        payload=facts,
        occurred_at=NOW + timedelta(seconds=offset),
    )


@pytest.mark.asyncio
async def test_import_deduplication_cursor_attribution_and_restart(context):
    module, store, path = context
    facts, snapshot = batch(), account()
    assert isinstance(store, ObservationStorePort)
    first = await store.ingest(facts, snapshot, audit(facts))
    assert first.imported_count == 2
    assert first.account_revision == 1
    duplicate = await store.ingest(facts, snapshot, audit(facts))
    assert duplicate.imported_count == 0
    assert duplicate.duplicate_count == 2
    assert not duplicate.receipt.appended
    assert duplicate.receipt.sequence == first.receipt.sequence
    reopened = module.SqliteObservationStore(path)
    await reopened.initialize()
    assert await reopened.cursor("account-1", "BTCUSDT") == facts.next_cursor
    observed = await reopened.observed_trades("account-1", "BTCUSDT")
    assert observed == facts.trades
    attribution = TradeAttribution(account_ref="account-1", symbol="BTCUSDT", trade_id="t1")
    projection = await reopened.load(attribution.aggregate_id)
    assert projection.state.original_author == "unclassified"
    assert projection.state.executor == "human"
    assert not projection.state.user_confirmed


@pytest.mark.asyncio
async def test_same_trade_id_is_isolated_by_account_and_symbol(context):
    _, store, _ = context
    for account_ref, symbol in (("account-1", "BTCUSDT"), ("account-2", "ETHUSDT")):
        facts = batch(account_ref=account_ref, symbol=symbol)
        await store.ingest(facts, account(account_ref), audit(facts, account_ref))
    assert len(await store.observed_trades("account-1", "BTCUSDT")) == 2
    assert len(await store.observed_trades("account-2", "ETHUSDT")) == 2


@pytest.mark.asyncio
async def test_final_audit_failure_rolls_back_every_projection_and_cursor(context):
    _, store, path = context
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_import_audit BEFORE INSERT ON journal_events
            WHEN NEW.kind='trades_imported'
            BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END;
        """)
    facts = batch()
    with pytest.raises(PersistenceUnavailable):
        await store.ingest(facts, account(), audit(facts))
    assert not await store.observed_trades("account-1", "BTCUSDT")
    assert await store.cursor("account-1", "BTCUSDT") == TradeCursor()
    assert await store.account_snapshot("account-1") is None
    attribution = TradeAttribution(account_ref="account-1", symbol="BTCUSDT", trade_id="t1")
    assert await store.load(attribution.aggregate_id) is None
    assert not (await store.scan(0, 10)).records


@pytest.mark.asyncio
async def test_changed_trade_fact_is_rejected_without_partial_import(context):
    _, store, _ = context
    original = batch()
    await store.ingest(original, account(), audit(original))
    changed = batch((trade("t1", price="60001"), trade("t3", 3)))
    with pytest.raises(ObservationConflict):
        await store.ingest(changed, account(offset=6), audit(changed, "import-2", 6))


@pytest.mark.asyncio
async def test_legacy_missing_quote_does_not_block_inclusive_cursor_or_rewrite_old_fact(context):
    _, store, path = context
    original = batch((trade("123"),))
    await store.ingest(original, account(), audit(original))
    with sqlite3.connect(path) as connection:
        old_body = json.loads(connection.execute("SELECT body FROM observed_trades").fetchone()[0])
        del old_body["quote_quantity"]
        raw = json.dumps(old_body)
        connection.execute("UPDATE observed_trades SET body=?", (raw,))
    newer = batch(
        (trade("123").model_copy(update={"quote_quantity": Decimal("60")}), trade("124", 2))
    )
    # Validate model-copy inputs, as a production mapped fact would be validated.
    newer = TradeBatch.model_validate_json(newer.model_dump_json())
    result = await store.ingest(newer, account(offset=6), audit(newer, "import-new", 6))
    assert result.imported_count == 1 and result.duplicate_count == 1
    assert result.next_cursor.last_trade_id == "124"
    assert (await store.observed_trades("account-1", "BTCUSDT"))[0].quote_quantity is None
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT body FROM observed_trades WHERE trade_id='123'").fetchone()[
                0
            ]
            == raw
        )


@pytest.mark.asyncio
async def test_known_quote_conflict_is_rejected_even_if_other_fields_match(context):
    _, store, _ = context
    fact = trade("123").model_copy(update={"quote_quantity": Decimal("60")})
    original = TradeBatch.model_validate_json(batch((fact,)).model_dump_json())
    await store.ingest(original, account(), audit(original))
    changed = TradeBatch.model_validate_json(
        batch((fact.model_copy(update={"quote_quantity": Decimal("61")}),)).model_dump_json()
    )
    with pytest.raises(ObservationConflict):
        await store.ingest(changed, account(offset=6), audit(changed, "changed-quote", 6))


@pytest.mark.asyncio
async def test_first_supplemental_quote_is_immutable_across_restart_and_v5_upgrade(context):
    module, store, path = context
    original = batch((trade("123"),))
    await store.ingest(original, account(), audit(original))
    supplemented = batch((trade("123").model_copy(update={"quote_quantity": Decimal("60")}),))
    await store.ingest(supplemented, account(offset=6), audit(supplemented, "quote-proof", 6))
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS trade_quote_evidence")
        connection.execute("PRAGMA user_version=5")
    reopened = module.SqliteObservationStore(path)
    await reopened.initialize()
    conflicting = batch((trade("123").model_copy(update={"quote_quantity": Decimal("61")}),))
    with pytest.raises(ObservationConflict):
        await reopened.ingest(conflicting, account(offset=7), audit(conflicting, "bad-proof", 7))
    assert (await reopened.observed_trades("account-1", "BTCUSDT"))[0].quote_quantity is None


@pytest.mark.asyncio
async def test_failed_import_rolls_back_supplemental_quote_proof(context):
    _, store, path = context
    original = batch((trade("123"),))
    await store.ingest(original, account(), audit(original))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_quote BEFORE INSERT ON journal_events "
            "WHEN NEW.event_id='quote-failed' BEGIN SELECT RAISE(ABORT, 'fail'); END;"
        )
    proof = batch((trade("123").model_copy(update={"quote_quantity": Decimal("60")}),))
    with pytest.raises(PersistenceUnavailable):
        await store.ingest(proof, account(offset=6), audit(proof, "quote-failed", 6))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM trade_quote_evidence").fetchone()[0] == 0
    alternative = batch((trade("123").model_copy(update={"quote_quantity": Decimal("61")}),))
    assert (
        await store.ingest(alternative, account(offset=7), audit(alternative, "first-quote", 7))
    ).duplicate_count == 1


@pytest.mark.asyncio
async def test_v5_conflicting_quote_audits_abort_upgrade_and_preserve_backup(context):
    module, store, path = context
    original = batch((trade("123"),))
    await store.ingest(original, account(), audit(original))
    proof = batch((trade("123").model_copy(update={"quote_quantity": Decimal("60")}),))
    result = await store.ingest(proof, account(offset=6), audit(proof, "first-proof", 6))
    conflicting = batch((trade("123").model_copy(update={"quote_quantity": Decimal("61")}),))
    contradictory_event = audit(conflicting, "v5-second-proof", 7)
    receipt = await store.append(contradictory_event)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO import_results(event_id, account_input, body) VALUES(?,?,?)",
            (
                receipt.event_id,
                account(offset=7).model_dump_json(),
                result.model_copy(update={"receipt": receipt}).model_dump_json(),
            ),
        )
        connection.execute("DROP TABLE trade_quote_evidence")
        connection.execute("PRAGMA user_version=5")
    with pytest.raises(PersistenceUnavailable):
        await module.SqliteObservationStore(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT count(*) FROM journal_events WHERE event_id='v5-second-proof'"
            ).fetchone()[0]
            == 1
        )
    assert tuple(path.parent.glob(path.name + ".pre-v*-*.bak"))
    assert await store.observed_trades("account-1", "BTCUSDT") == original.trades
    assert await store.cursor("account-1", "BTCUSDT") == original.next_cursor


@pytest.mark.asyncio
async def test_cursor_cannot_reference_unknown_fact_or_regress(context):
    _, store, _ = context
    unknown = batch(next_cursor=TradeCursor(last_trade_id="unseen"))
    with pytest.raises(ObservationConflict):
        await store.ingest(unknown, account(), audit(unknown))
    assert not await store.observed_trades("account-1", "BTCUSDT")
    original = batch()
    await store.ingest(original, account(), audit(original))
    earlier = batch(
        next_cursor=TradeCursor(last_trade_id="t1", last_executed_at=original.trades[0].executed_at)
    )
    with pytest.raises(ObservationConflict):
        await store.ingest(earlier, account(offset=6), audit(earlier, "import-2", 6))
    assert await store.cursor("account-1", "BTCUSDT") == original.next_cursor


@pytest.mark.asyncio
async def test_account_revision_tracks_balances_not_sampling_time_or_provider_revision(context):
    _, store, _ = context
    empty = batch(())
    first = await store.ingest(empty, account(), audit(empty))
    refreshed = await store.ingest(
        empty, account(offset=6, revision=999), audit(empty, "refresh", 6)
    )
    changed = await store.ingest(
        empty, account(offset=7, quantity="0.02"), audit(empty, "changed", 7)
    )
    assert first.account_revision == refreshed.account_revision == 1
    assert changed.account_revision == 2
    assert (await store.account_snapshot("account-1")).account_revision == 2
    with pytest.raises(ObservationConflict):
        await store.ingest(empty, account(offset=5), audit(empty, "older", 8))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "stale"])
async def test_failed_refresh_preserves_verified_balances_version_and_data_time(context, status):
    _, store, _ = context
    empty = batch(())
    await store.ingest(empty, account(), audit(empty))
    failed = AccountSnapshot(
        account_ref="account-1", balances=(), as_of=NOW + timedelta(seconds=6), status=status
    )
    await store.ingest(empty, failed, audit(empty, "outage", 6))
    cached = await store.account_snapshot("account-1")
    assert cached.balances == account().balances
    assert cached.account_revision == 1
    assert cached.as_of == account().as_of
    assert cached.status == status
    events = (await store.scan(0, 10)).records
    outage = next(item.event for item in events if item.event.event_id == "account-observed:outage")
    assert outage.occurred_at == failed.as_of
    refreshed = await store.ingest(empty, account(offset=7), audit(empty, "recovered", 7))
    assert refreshed.account_revision == 1


@pytest.mark.asyncio
async def test_initial_unavailable_account_has_no_verified_balance_revision(context):
    _, store, _ = context
    empty = batch(())
    unknown = AccountSnapshot(account_ref="account-1", as_of=NOW, status="unavailable")
    result = await store.ingest(empty, unknown, audit(empty))
    assert result.account_revision == 0
    result = await store.ingest(empty, account(quantity="0"), audit(empty, "first-fresh"))
    assert result.account_revision == 1


@pytest.mark.asyncio
async def test_event_identity_cannot_hide_a_new_account_read(context):
    _, store, _ = context
    facts = batch()
    await store.ingest(facts, account(), audit(facts))
    with pytest.raises(EventIdentityConflict):
        await store.ingest(facts, account(quantity="0.02"), audit(facts))


@pytest.mark.asyncio
async def test_scope_and_future_facts_are_rejected_before_writing(context):
    _, store, _ = context
    facts = batch()
    with pytest.raises(ValueError):
        await store.ingest(facts, account("account-2"), audit(facts))
    with pytest.raises(ValueError):
        await store.ingest(facts, account(), audit(facts, offset=1))
    assert not await store.observed_trades("account-1", "BTCUSDT")
