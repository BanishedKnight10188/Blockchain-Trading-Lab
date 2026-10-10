"""Pagination is bounded, exact, resumable and refuses changed or reordered facts."""

import importlib

import pytest

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.account import TradeCursor
from tests.adapters.test_account_mapping import trade_wire
from tests.domain.test_decisions import NOW
from tests.market.test_normalizer import MS


def module():
    return importlib.import_module("agent_platform.adapters.binance_direct.trade_history")


class Pages:
    def __init__(self, pages):
        self.pages, self.queries = iter(pages), []

    async def request(self, method, path, params):
        assert method == "GET" and path == "/api/v3/myTrades"
        self.queries.append(dict(params))
        return next(self.pages)


@pytest.mark.asyncio
async def test_pages_repeat_boundary_once_and_keep_exact_last_cursor():
    source = Pages(
        [[trade_wire(1), trade_wire(2)], [trade_wire(2), trade_wire(3)], [trade_wire(3)]]
    )
    result = (
        await module()
        .TradeHistory(source, FakeClock(NOW), page_size=2)
        .read("account-1", "BTCUSDT", TradeCursor())
    )
    assert [q["fromId"] for q in source.queries] == [0, 2, 3]
    assert [fact.trade_id for fact in result.trades] == ["1", "2", "3"]
    assert result.next_cursor.last_trade_id == "3"
    assert result.history_complete


@pytest.mark.asyncio
async def test_page_cap_keeps_partial_history_and_can_resume_from_inclusive_cursor():
    source = Pages([[trade_wire(1), trade_wire(2)]])
    result = (
        await module()
        .TradeHistory(source, FakeClock(NOW), page_size=2, max_pages=1)
        .read("account-1", "BTCUSDT", TradeCursor())
    )
    assert not result.history_complete
    next_source = Pages([[trade_wire(2), trade_wire(3)], [trade_wire(3)]])
    next_batch = (
        await module()
        .TradeHistory(next_source, FakeClock(NOW), page_size=2)
        .read("account-1", "BTCUSDT", result.next_cursor)
    )
    assert next_source.queries[0]["fromId"] == 2
    assert next_batch.next_cursor.last_trade_id == "3"
    assert not next_batch.history_complete


@pytest.mark.asyncio
async def test_empty_incremental_result_preserves_old_cursor():
    cursor = TradeCursor(last_trade_id="9", last_executed_at=NOW)
    result = (
        await module()
        .TradeHistory(Pages([[]]), FakeClock(NOW))
        .read("account-1", "BTCUSDT", cursor)
    )
    assert not result.trades and result.next_cursor == cursor and not result.history_complete


@pytest.mark.parametrize(
    "pages",
    [
        [[trade_wire(2), trade_wire(1)]],
        [[trade_wire(1), trade_wire(2)], [trade_wire(2, commission="9")]],
        [[trade_wire(1, time=MS + 1)]],
        [[trade_wire(1, time=MS), trade_wire(2, time=MS - 1)]],
        [[trade_wire(1), trade_wire(2)], [trade_wire(1), trade_wire(2)]],
        [[trade_wire(1), trade_wire(1)]],
        [{}],
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_conflicting_page_never_returns_partial_batch(pages):
    with pytest.raises(module().TradeHistoryError):
        await (
            module()
            .TradeHistory(Pages(pages), FakeClock(NOW), page_size=2)
            .read("account-1", "BTCUSDT", TradeCursor())
        )


@pytest.mark.parametrize(
    "cursor,symbol",
    [
        (TradeCursor(last_trade_id="opaque-id"), "BTCUSDT"),
        (TradeCursor(last_trade_id="01"), "BTCUSDT"),
        (TradeCursor(last_executed_at=NOW), "BTCUSDT"),
        (TradeCursor(), "ETHUSDT"),
    ],
)
@pytest.mark.asyncio
async def test_bad_scope_or_cursor_fails_before_fetch(cursor, symbol):
    source = Pages([])
    with pytest.raises(module().TradeHistoryError):
        await module().TradeHistory(source, FakeClock(NOW)).read("account-1", symbol, cursor)
    assert not source.queries
