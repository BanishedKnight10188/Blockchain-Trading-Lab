"""Selected-symbol public futures reads; no account or execution capabilities."""

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from agent_platform.domain.futures_market import (
    FuturesContractRules,
    FuturesFundingWindow,
    FuturesMarketSnapshot,
)
from agent_platform.domain.futures_values import FuturesFunding, FuturesQuote
from agent_platform.domain.session_market import PerpetualContract, SessionAnalysisTarget
from agent_platform.ports.futures_market import FuturesMarketUnavailable

from .futures_public import FuturesPublicClient, FuturesPublicError, _millis


def _number(value):
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise ValueError("invalid exchange numeric string")
    return value


def _symbol(symbol):
    return SessionAnalysisTarget(market="usdt_perpetual", symbol=symbol).symbol


def _timestamp(value):
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


class FuturesMarketClient:
    def __init__(self, clock, *, transport=None, proxy_url=None):
        self.clock = clock
        self._public = FuturesPublicClient(clock, transport=transport, proxy_url=proxy_url)
        self._rules = OrderedDict()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        await self._public.aclose()

    async def snapshot(self, symbol):
        symbol = _symbol(symbol)
        try:
            async with asyncio.timeout(5):
                mark = await self._public.get("/fapi/v1/premiumIndex", {"symbol": symbol})
                mark_received = self.clock.utcnow()
                if type(mark) is not dict or mark["symbol"] != symbol:
                    raise ValueError("invalid contract mark")
                mark_at = _millis(mark["time"])
                if not timedelta(0) <= mark_received - mark_at <= timedelta(seconds=5):
                    raise ValueError("mark was future or stale at first reception")
                book = await self._public.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
            if type(mark) is not dict or type(book) is not dict:
                raise ValueError("invalid futures quote shape")
            if mark["symbol"] != symbol or book["symbol"] != symbol:
                raise ValueError("wrong contract quote")
            quote = FuturesQuote(
                symbol=symbol,
                source="binance_futures_public",
                bid=_number(book["bidPrice"]),
                ask=_number(book["askPrice"]),
                mark=_number(mark["markPrice"]),
                book_at=_millis(book["time"]),
                mark_at=mark_at,
                received_at=self.clock.utcnow(),
            )
            return FuturesMarketSnapshot(
                quote=quote,
                index_price=_number(mark["indexPrice"]),
                displayed_funding_rate=_number(mark["lastFundingRate"]),
                next_funding_at=_millis(mark["nextFundingTime"]),
                bid_quantity=_number(book["bidQty"]),
                ask_quantity=_number(book["askQty"]),
            )
        except (FuturesPublicError, ValueError, TypeError, KeyError, OverflowError, TimeoutError):
            raise FuturesMarketUnavailable("futures_quote_unavailable") from None

    async def rules(self, symbol):
        symbol = _symbol(symbol)
        cached = self._rules.get(symbol)
        if cached is not None and 0 <= self.clock.monotonic() - cached[1] < 300:
            self._rules.move_to_end(symbol)
            return cached[0]
        try:
            result = await self._public.get("/fapi/v1/exchangeInfo")
            rows = result["symbols"]
            if (
                type(rows) is not list
                or len(rows) > 10000
                or any(type(row) is not dict for row in rows)
            ):
                raise ValueError("invalid contract catalog")
            matches = [row for row in rows if row.get("symbol") == symbol]
            if len(matches) != 1:
                raise ValueError("ambiguous or unavailable contract")
            item = matches[0]
            if (
                item.get("contractType"),
                item.get("status"),
                item.get("quoteAsset"),
                item.get("marginAsset"),
            ) != ("PERPETUAL", "TRADING", "USDT", "USDT"):
                raise ValueError("contract is not available for USDT perpetual trading")
            filters = item["filters"]
            if type(filters) is not list or not 1 <= len(filters) <= 32:
                raise ValueError("invalid contract filters")
            owned = {}
            for entry in filters:
                if type(entry) is not dict or type(entry.get("filterType")) is not str:
                    raise ValueError("invalid contract filter")
                if entry["filterType"] in owned:
                    raise ValueError("duplicate contract filter")
                owned[entry["filterType"]] = entry
            price, lot, market, notional, percent = (
                owned[key]
                for key in (
                    "PRICE_FILTER",
                    "LOT_SIZE",
                    "MARKET_LOT_SIZE",
                    "MIN_NOTIONAL",
                    "PERCENT_PRICE",
                )
            )
            rules = FuturesContractRules(
                contract=PerpetualContract(symbol=symbol, base_asset=item["baseAsset"]),
                captured_at=self.clock.utcnow(),
                price_tick=_number(price["tickSize"]),
                min_price=_number(price["minPrice"]),
                max_price=_number(price["maxPrice"]),
                lot_step=_number(lot["stepSize"]),
                lot_min_qty=_number(lot["minQty"]),
                lot_max_qty=_number(lot["maxQty"]),
                market_step=_number(market["stepSize"]),
                market_min_qty=_number(market["minQty"]),
                market_max_qty=_number(market["maxQty"]),
                min_notional=_number(notional["notional"]),
                percent_down=_number(percent["multiplierDown"]),
                percent_up=_number(percent["multiplierUp"]),
                market_take_bound=_number(item["marketTakeBound"]),
            )
        except (FuturesPublicError, ValueError, TypeError, KeyError, OverflowError):
            raise FuturesMarketUnavailable("futures_rules_unavailable") from None
        self._rules[symbol] = (rules, self.clock.monotonic())
        self._rules.move_to_end(symbol)
        while len(self._rules) > 32:
            self._rules.popitem(last=False)
        return rules

    async def settlements(self, symbol, *, after, through=None):
        symbol = _symbol(symbol)
        now = self.clock.utcnow()
        through = (
            through
            if through is not None
            else now.replace(microsecond=now.microsecond // 1000 * 1000)
        )
        window = FuturesFundingWindow(
            symbol=symbol,
            requested_after=after,
            requested_through=through,
            captured_at=now,
            events=(),
        )
        cursor, end = _timestamp(window.requested_after) + 1, _timestamp(window.requested_through)
        events = []
        try:
            for _ in range(4):
                if cursor > end:
                    break
                rows = await self._public.get(
                    "/fapi/v1/fundingRate",
                    {
                        "symbol": symbol,
                        "startTime": cursor,
                        "endTime": end,
                        "limit": 1000,
                    },
                )
                if type(rows) is not list or len(rows) > 1000:
                    raise ValueError("invalid funding page")
                for item in rows:
                    if type(item) is not dict or item.get("rateType") != "Regular":
                        raise FuturesMarketUnavailable("unsupported_funding_event")
                    event = FuturesFunding(
                        symbol=item["symbol"],
                        source="binance_futures_public",
                        rate=_number(item["fundingRate"]),
                        mark=_number(item["markPrice"]),
                        settled_at=_millis(item["fundingTime"]),
                    )
                    stamp = _timestamp(event.settled_at)
                    if event.symbol != symbol or not cursor <= stamp <= end:
                        raise ValueError("funding page crossed its cursor or identity")
                    cursor = stamp + 1
                    events.append(event)
                if len(rows) < 1000 or cursor > end:
                    break
            else:
                raise FuturesMarketUnavailable("funding_window_capacity_exceeded")
            return FuturesFundingWindow(
                symbol=symbol,
                requested_after=after,
                requested_through=through,
                captured_at=self.clock.utcnow(),
                events=tuple(events),
            )
        except (FuturesPublicError, ValueError, TypeError, KeyError, OverflowError):
            raise FuturesMarketUnavailable("futures_funding_unavailable") from None
