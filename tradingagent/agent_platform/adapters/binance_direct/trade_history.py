"""Bounded inclusive-ID REST pagination. An exhausted API history is not a cost basis."""

import re

from agent_platform.adapters.binance_direct.account_mapping import map_trade
from agent_platform.adapters.binance_direct.read_client import ReadOnlyClient
from agent_platform.domain.account import TradeBatch, TradeCursor
from agent_platform.domain.common import live_account_ref
from agent_platform.ports.clock import ClockPort


class TradeHistoryError(ValueError):
    pass


def _cursor_id(cursor: TradeCursor) -> int:
    if cursor.last_trade_id is None:
        if cursor.last_executed_at is not None:
            raise ValueError("time-only cursor is unsupported")
        return 0
    value = cursor.last_trade_id
    if re.fullmatch(r"0|[1-9][0-9]{0,18}", value) is None or int(value) > 2**63 - 1:
        raise ValueError("invalid exchange cursor")
    if cursor.last_executed_at is None:
        raise ValueError("identified cursor requires confirmed execution time")
    return int(value)


class TradeHistory:
    def __init__(
        self, client: ReadOnlyClient, clock: ClockPort, *, page_size: int = 1000, max_pages: int = 4
    ):
        if type(page_size) is not int or not 2 <= page_size <= 1000:
            raise ValueError("trade page size must be 2..1000")
        if type(max_pages) is not int or not 1 <= max_pages <= 20:
            raise ValueError("trade fetch page count must be 1..20")
        self.client, self.clock = client, clock
        self.page_size, self.max_pages = page_size, max_pages

    async def read(self, account_ref: str, symbol: str, cursor: TradeCursor) -> TradeBatch:
        try:
            ref = live_account_ref(account_ref)
            if symbol != "BTCUSDT":
                raise ValueError("unsupported trade symbol")
            lower = _cursor_id(cursor)
            origin = cursor.last_trade_id is None
            facts = {}
            last_time = cursor.last_executed_at
            next_cursor = cursor
            exhausted = False
            for _ in range(self.max_pages):
                rows = await self.client.request(
                    "GET",
                    "/api/v3/myTrades",
                    {"symbol": symbol, "fromId": lower, "limit": self.page_size},
                )
                if type(rows) is not list or len(rows) > self.page_size:
                    raise ValueError("invalid trade page shape")
                previous_id, new_last = None, lower
                for row in rows:
                    fact = map_trade(row, ref)
                    identity = int(fact.trade_id)
                    if identity < lower or (previous_id is not None and identity <= previous_id):
                        raise ValueError("trade IDs are not strictly ordered")
                    previous_id = identity
                    if fact.executed_at > self.clock.utcnow() or (
                        last_time is not None and fact.executed_at < last_time
                    ):
                        raise ValueError("trade execution time is future or regressive")
                    if (
                        fact.trade_id == cursor.last_trade_id
                        and fact.executed_at != cursor.last_executed_at
                    ):
                        raise ValueError("cursor boundary time changed")
                    if fact.trade_id in facts and facts[fact.trade_id] != fact:
                        raise ValueError("trade boundary fact changed")
                    facts[fact.trade_id] = fact
                    new_last, last_time = identity, fact.executed_at
                    next_cursor = TradeCursor(
                        last_trade_id=fact.trade_id, last_executed_at=last_time
                    )
                if len(rows) < self.page_size:
                    exhausted = True
                    break
                if new_last <= lower:
                    raise ValueError("trade pagination did not advance")
                lower = new_last
            return TradeBatch(
                account_ref=ref,
                symbol=symbol,
                trades=tuple(facts.values()),
                next_cursor=next_cursor,
                history_complete=origin and exhausted,
            )
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise TradeHistoryError("invalid Binance Spot trade history") from None
