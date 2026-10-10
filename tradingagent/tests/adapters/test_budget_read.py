"""A status query sees durable unknown exposure without changing the ledger."""

from decimal import Decimal

import pytest

from tests.adapters.test_sqlite_budgets import context as _context
from tests.adapters.test_sqlite_budgets import request, usage
from tests.domain.test_cost_contracts import NOW

context = _context


@pytest.mark.asyncio
async def test_read_budget_preserves_unknown_exposure_and_records_no_new_call(context):
    _, store, _ = context
    reservation = await store.reserve(request())
    await store.settle(reservation.reservation_id, usage(reservation))
    balance = await store.budget_balance(NOW, daily_limit_usd=Decimal("1"))
    assert balance.reserved_usd == reservation.request.estimated_cost_usd
    assert balance.spent_usd == 0 and balance.hourly_call_count == 1
    assert await store.budget_balance(NOW, daily_limit_usd=Decimal("1")) == balance
