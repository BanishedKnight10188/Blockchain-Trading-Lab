"""Real SQLite atomic claims, late responses and rollback of virtual funds."""

import asyncio
import importlib
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.adapters.sqlite.store import open_store
from agent_platform.domain.agent_controls import AgentControlState
from agent_platform.domain.events import JournalEvent, SessionJournalEvent, StateRecord
from agent_platform.domain.model_modules import JevModuleSettings, JevTraderSettings
from agent_platform.domain.operating_modes import OperatingSettings
from agent_platform.domain.paper import PaperFill
from agent_platform.domain.sessions import AgentSession, TradingStyle
from agent_platform.ports.sessions import PersistenceUnavailable, RevisionConflict
from tests.domain.test_paper_trading import NOW, settings_data


@pytest_asyncio.fixture
async def context(tmp_path):
    module = importlib.import_module("agent_platform.adapters.sqlite.paper")
    domain = importlib.import_module("agent_platform.domain.paper_trading")
    path = tmp_path / "agent.sqlite3"
    core = await open_store(path)
    sessions = SqliteSessionStore(path)
    session = AgentSession(
        session_id="session-1", style=TradingStyle(strength=35), created_at=NOW, updated_at=NOW
    )
    await sessions.create(
        session,
        SessionJournalEvent(
            event_id="session-created", kind="created", session=session, occurred_at=NOW
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
    store = module.SqlitePaperStore(path)
    policy = domain.PaperSettings(**settings_data())
    account = await store.create(session.session_id, policy, NOW)
    return module, domain, store, core, sessions, account, path


async def write_controls(core, controls):
    record = StateRecord(
        key="agent-controls",
        revision=controls.revision,
        state_type="agent_controls",
        state=controls,
        updated_at=controls.updated_at,
    )
    await core.save(
        record,
        controls.revision - 1,
        JournalEvent(
            event_id=f"controls-{controls.revision}",
            aggregate_id=record.key,
            kind="state_changed",
            payload=record,
            occurred_at=record.updated_at,
        ),
    )


async def running(context):
    _, _, store, _, _, account, _ = context
    return await store.start(account.account_ref, account.revision, NOW)


def fill(request_id="cycle-1"):
    return PaperFill(
        mode="paper",
        fill_id=f"paper-fill:{request_id}",
        intent_id=request_id,
        account_ref="paper:session-1",
        symbol="BTCUSDT",
        side="buy",
        quantity="0.001",
        price="60000",
        fee="0.06",
        fee_asset="USDT",
        slippage_bps="2",
        model_version="paper-top-of-book-v1",
        filled_at=NOW + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_create_reopen_no_recharge_and_audit(context):
    module, _, store, core, _, account, path = context
    assert await module.SqlitePaperStore(path).get(account.account_ref) == account
    with pytest.raises(RevisionConflict):
        await store.create(account.session_id, account.settings, NOW)
    assert (await store.get(account.account_ref)).usdt == 1000
    record = await core.load(account.account_ref)
    assert record.state_type == "paper_account"


@pytest.mark.asyncio
async def test_two_writers_claim_only_once_and_fill_is_idempotent(context):
    module, _, store, _, _, _, path = context
    account = await running(context)
    other = module.SqlitePaperStore(path)
    results = await asyncio.gather(
        *[
            s.claim(
                account.account_ref,
                f"cycle-{i}",
                account.revision,
                NOW,
                NOW + timedelta(seconds=15),
            )
            for i, s in enumerate((store, other), 1)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, RevisionConflict) for r in results) == 1
    cycle = next(r for r in results if not isinstance(r, Exception))
    result = await store.complete(
        account.account_ref,
        cycle.request_id,
        "BUY",
        "0.9",
        fill(cycle.request_id),
        None,
        NOW + timedelta(seconds=1),
    )
    wallet = await store.get(account.account_ref)
    assert (wallet.usdt, wallet.btc) == (Decimal("939.94"), Decimal("0.001"))
    assert (
        await store.complete(
            account.account_ref,
            cycle.request_id,
            "BUY",
            "0.9",
            fill(cycle.request_id),
            None,
            NOW + timedelta(seconds=1),
        )
        == result
    )
    assert await store.get(account.account_ref) == wallet


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "style", "trader", "close", "deadline"])
async def test_late_result_cannot_change_balances(context, change):
    _, _, store, core, sessions, _, _ = context
    account = await running(context)
    cycle = await store.claim(
        account.account_ref, "cycle-1", account.revision, NOW, NOW + timedelta(seconds=15)
    )
    at = NOW + timedelta(seconds=1)
    if change == "pause":
        current = await store.get(account.account_ref)
        await store.pause(account.account_ref, current.revision, at)
    elif change in ("style", "close"):
        session = await sessions.get(account.session_id)
        changed = (
            session.change_style(TradingStyle(strength=90), at)
            if change == "style"
            else session.transition("closed", at)
        )
        await sessions.save(
            changed,
            session.revision,
            SessionJournalEvent(
                event_id="session-changed",
                kind="style_changed" if change == "style" else "state_changed",
                session=changed,
                occurred_at=at,
            ),
        )
    elif change == "trader":
        previous = (await core.load("agent-controls")).state
        changed = AgentControlState(
            **(
                previous.model_dump()
                | {
                    "revision": 2,
                    "trader_revision": 2,
                    "updated_at": at,
                    "trader": JevTraderSettings(enabled=False),
                }
            )
        )
        await write_controls(core, changed)
    else:
        at = NOW + timedelta(seconds=16)
    result = await store.complete(
        account.account_ref, cycle.request_id, "BUY", "0.9", fill(), None, at
    )
    assert result.status == "discarded"
    assert (await store.get(account.account_ref)).usdt == 1000
    assert (await store.get(account.account_ref)).btc == 0


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_cycle_and_funds(context):
    _, _, store, _, _, _, path = context
    account = await running(context)
    cycle = await store.claim(
        account.account_ref, "cycle-1", account.revision, NOW, NOW + timedelta(seconds=15)
    )
    before = await store.get(account.account_ref)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER deny_paper_audit BEFORE INSERT ON journal_events "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    with pytest.raises(PersistenceUnavailable):
        await store.complete(
            account.account_ref,
            cycle.request_id,
            "BUY",
            "0.9",
            fill(),
            None,
            NOW + timedelta(seconds=1),
        )
    assert await store.get(account.account_ref) == before
    assert (await store.recent(account.account_ref))[0].status == "pending"


@pytest.mark.asyncio
async def test_recovery_pauses_without_losing_wallet_or_replaying(context):
    module, _, store, _, _, _, path = context
    account = await running(context)
    await store.claim(
        account.account_ref, "cycle-1", account.revision, NOW, NOW + timedelta(seconds=15)
    )
    reopened = module.SqlitePaperStore(path)
    await reopened.recover(NOW + timedelta(seconds=1))
    recovered = await reopened.get(account.account_ref)
    assert recovered.status == "paused" and recovered.pending_request_id is None
    assert recovered.usdt == 1000 and recovered.btc == 0
    assert (await reopened.recent(account.account_ref))[0].status == "discarded"


@pytest.mark.asyncio
async def test_pause_survives_wallet_updates_but_cannot_cross_new_activation(context):
    _, _, store, _, _, _, _ = context
    started = await running(context)
    cycle = await store.claim(
        started.account_ref, "cycle-1", started.revision, NOW, NOW + timedelta(seconds=15)
    )
    await store.complete(started.account_ref, cycle.request_id, "WAIT", "0.9", None, None, NOW)
    paused = await store.pause(
        started.account_ref,
        started.revision,
        NOW,
        expected_activation_revision=started.activation_revision,
    )
    assert paused.status == "paused"
    restarted = await store.start(paused.account_ref, paused.revision, NOW)
    with pytest.raises(RevisionConflict):
        await store.pause(
            restarted.account_ref,
            restarted.revision,
            NOW,
            expected_activation_revision=started.activation_revision,
        )
