"""Provider-managed limits retain exact fees without a second local money cap."""

import sqlite3
from decimal import Decimal

import pytest

from tests.adapters.test_sqlite_budgets import context as _context
from tests.adapters.test_sqlite_budgets import request, usage

context = _context


@pytest.mark.asyncio
async def test_provider_managed_requests_record_fees_beyond_old_local_cap(context):
    _, store, _ = context
    first = await store.reserve(request("old"))
    await store.settle(first.reservation_id, usage(first))
    new = await store.reserve(request("new", provider_managed=True, session_id="session-new"))
    result = await store.settle(new.reservation_id, usage(new, "0.8"))
    assert result.spent_usd == Decimal("0.8")
    assert result.reserved_usd == Decimal("0.6")
    assert not result.billing_frozen and result.provider_managed
    again = await store.reserve(request("again", provider_managed=True))
    assert again.request.request_id == "again"


@pytest.mark.asyncio
async def test_session_spend_includes_legacy_jev_background_and_unknown_fees(context, tmp_path):
    _, store, _ = context
    legacy = await store.reserve(request("legacy", estimated_cost_usd="0.1"))
    await store.settle(legacy.reservation_id, usage(legacy, "0.04"))
    background = await store.reserve(request("background", estimated_cost_usd="0.1"))
    await store.settle(background.reservation_id, usage(background, "0.03"))
    pending = await store.reserve(request("pending", estimated_cost_usd="0.1", session_id="one"))
    await store.settle(pending.reservation_id, usage(pending))
    await store.reserve(request("other", estimated_cost_usd="0.1", session_id="two"))
    wallet = tmp_path / "task.sqlite3"
    with sqlite3.connect(wallet) as db:
        db.execute("CREATE TABLE futures_trading_cycles(request_id TEXT,body TEXT)")
        db.execute(
            "INSERT INTO futures_trading_cycles VALUES (?,?)",
            ("legacy", '{"scope":{"session_id":"one"}}'),
        )
    with sqlite3.connect(wallet.with_suffix(".background.sqlite3")) as db:
        db.execute("CREATE TABLE background_records(request_id TEXT,session_id TEXT)")
        db.execute("INSERT INTO background_records VALUES (?,?)", ("background", "one"))
    result = await store.session_usage("one", wallet_database=wallet)
    assert result == {"spent_usd": "0.07", "unconfirmed_usd": "0.1", "call_count": 3}


@pytest.mark.asyncio
async def test_provider_managed_decision_records_session_without_local_money_cap(tmp_path):
    from agent_platform.domain.decision_models import DecisionModelRequest
    from tests.application.test_decision_models import setup, valid_response

    class Port:
        async def decide(self, request, quote):
            return valid_response(request, quote)

    worker, store, original = await setup(tmp_path, daily="0", port=Port())
    worker.provider_managed = True
    worker.max_single_cost_usd = Decimal(0)
    data = original.model_dump()
    data["state_json"] = '{"account":{"scope":{"session_id":"one"}}}'
    response = await worker.decide(DecisionModelRequest(**data))
    assert response.usage.billing_status == "confirmed"
    assert await store.session_usage("one") == {
        "spent_usd": "0.0000084", "unconfirmed_usd": "0", "call_count": 1,
    }
