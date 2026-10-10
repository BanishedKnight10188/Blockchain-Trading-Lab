"""Public futures facts retain exchange clocks, identities and settled fee evidence."""

import asyncio
import importlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from agent_platform.adapters.binance_direct.futures_public import FuturesPublicClient
from agent_platform.adapters.fake.clock import FakeClock

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
MS = 1791374400000


def adapter():
    return importlib.import_module("agent_platform.adapters.binance_direct.futures_market")


def premium(symbol="ETHUSDT"):
    return {
        "symbol": symbol,
        "markPrice": "2000.12345678",
        "indexPrice": "2000.1",
        "estimatedSettlePrice": "2000.12",
        "lastFundingRate": "0.001",
        "interestRate": "0.0001",
        "nextFundingTime": MS + 3600000,
        "time": MS - 1000,
    }


def book(symbol="ETHUSDT"):
    return {
        "symbol": symbol,
        "bidPrice": "1999",
        "askPrice": "2001",
        "bidQty": "10",
        "askQty": "12.5",
        "time": MS - 2000,
    }


def transport(mark=None, ticker=None, paths=None):
    def handle(request):
        assert request.method == "GET" and request.url.host == "fapi.binance.com"
        assert "authorization" not in request.headers and "x-mbx-apikey" not in request.headers
        assert set(request.url.params) == {"symbol"}
        if paths is not None:
            paths.append(request.url.path)
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json=premium() if mark is None else mark)
        assert request.url.path == "/fapi/v1/ticker/bookTicker"
        return httpx.Response(200, json=book() if ticker is None else ticker)

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["ETHUSDT", "币安人生USDT", "1000SHIBUSDT"])
async def test_snapshot_uses_selected_public_contract_and_independent_exchange_times(symbol):
    paths = []
    async with adapter().FuturesMarketClient(
        FakeClock(NOW), transport=transport(premium(symbol), book(symbol), paths)
    ) as client:
        result = await client.snapshot(symbol)
    assert result.quote.symbol == symbol and result.quote.market == "usdt_perpetual"
    assert result.quote.source == "binance_futures_public"
    assert result.quote.mark == Decimal("2000.12345678")
    assert (result.quote.bid, result.quote.ask) == (Decimal("1999"), Decimal("2001"))
    assert result.quote.mark_at == NOW - timedelta(seconds=1)
    assert result.quote.book_at == NOW - timedelta(seconds=2)
    assert result.quote.received_at == NOW
    assert result.index_price == Decimal("2000.1")
    assert result.displayed_funding_rate == Decimal("0.001")
    assert result.next_funding_at == NOW + timedelta(hours=1)
    assert result.bid_quantity == 10 and result.ask_quantity == Decimal("12.5")
    assert paths == ["/fapi/v1/premiumIndex", "/fapi/v1/ticker/bookTicker"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "which,key,value",
    [
        ("mark", "symbol", "BTCUSDT"),
        ("book", "symbol", "BTCUSDT"),
        ("mark", "time", MS + 1),
        ("book", "time", MS + 1),
        ("mark", "time", MS - 5001),
        ("book", "time", MS - 5001),
        ("mark", "time", float(MS)),
        ("mark", "markPrice", "0"),
        ("mark", "markPrice", 2000.1),
        ("mark", "markPrice", "NaN"),
        ("mark", "markPrice", "1e1000000"),
        ("mark", "indexPrice", None),
        ("mark", "lastFundingRate", "Infinity"),
        ("mark", "nextFundingTime", MS - 2000),
        ("book", "bidPrice", "2002"),
        ("book", "askPrice", "0"),
        ("book", "bidQty", "0"),
        ("book", "askQty", "-1"),
        ("book", "askQty", True),
    ],
)
async def test_snapshot_rejects_invalid_or_stale_wire_facts(which, key, value):
    mod = adapter()
    mark, ticker = premium(), book()
    (mark if which == "mark" else ticker)[key] = value
    async with mod.FuturesMarketClient(FakeClock(NOW), transport=transport(mark, ticker)) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.snapshot("ETHUSDT")


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [[], [premium()], {"symbol": "ETHUSDT"}, None])
async def test_snapshot_missing_or_array_response_never_falls_back_to_spot(shape):
    mod = adapter()
    async with mod.FuturesMarketClient(
        FakeClock(NOW),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=shape)),
    ) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.snapshot("ETHUSDT")


@pytest.mark.asyncio
async def test_network_read_time_does_not_make_old_mark_fresh():
    mod, clock = adapter(), FakeClock(NOW)

    def handle(request):
        if request.url.path.endswith("premiumIndex"):
            return httpx.Response(200, json=premium())
        clock.advance_to(NOW + timedelta(seconds=5))
        return httpx.Response(200, json=book())

    async with mod.FuturesMarketClient(clock, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.snapshot("ETHUSDT")


@pytest.mark.asyncio
async def test_market_lane_is_not_blocked_by_historical_catalog_read():
    started, release = asyncio.Event(), asyncio.Event()

    async def historical(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"symbols": []})

    async with FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(historical)
    ) as history:
        stalled = asyncio.create_task(history.get("/fapi/v1/exchangeInfo"))
        await started.wait()
        try:
            async with adapter().FuturesMarketClient(
                FakeClock(NOW), transport=transport()
            ) as market:
                result = await asyncio.wait_for(market.snapshot("ETHUSDT"), timeout=1)
            assert result.quote.mark == Decimal("2000.12345678")
        finally:
            release.set()
            await stalled


@pytest.mark.asyncio
async def test_cancellation_releases_public_lane_for_next_quote():
    mod, entered = adapter(), asyncio.Event()
    first = True

    async def handle(request):
        nonlocal first
        if first:
            first = False
            entered.set()
            await asyncio.Event().wait()
        return httpx.Response(
            200, json=premium() if request.url.path.endswith("premiumIndex") else book()
        )

    async with mod.FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(handle)
    ) as client:
        task = asyncio.create_task(client.snapshot("ETHUSDT"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await asyncio.wait_for(client.snapshot("ETHUSDT"), 1)).quote.bid == 1999


@pytest.mark.asyncio
async def test_rate_limit_cooldown_blocks_new_http_without_retry():
    mod, clock, count = adapter(), FakeClock(NOW), 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"retry-after": "60"})

    async with mod.FuturesMarketClient(clock, transport=httpx.MockTransport(handle)) as client:
        for _ in range(2):
            with pytest.raises(mod.FuturesMarketUnavailable):
                await client.snapshot("ETHUSDT")
        assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,params",
    [
        ("/fapi/v1/premiumIndex", {}),
        ("/fapi/v1/ticker/bookTicker", {"symbol": "ethusdt"}),
        ("/fapi/v1/premiumIndex", {"symbol": "ETHUSDT", "signature": "blocked"}),
        ("/fapi/v1/order", {"symbol": "ETHUSDT"}),
    ],
)
async def test_public_wire_guard_rejects_ambiguous_or_private_requests_before_http(path, params):
    def fail_http(request):
        pytest.fail("invalid request reached public transport")

    async with FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(fail_http)
    ) as client:
        with pytest.raises(ValueError):
            await client.get(path, params)


def contract(symbol="ETHUSDT"):
    return {
        "symbol": symbol,
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "pricePrecision": 8,
        "quantityPrecision": 7,
        "marketTakeBound": "0.3",
        "maintMarginPercent": "999",
        "requiredMarginPercent": "999",
        "filters": [
            {
                "filterType": "PRICE_FILTER",
                "minPrice": "0.01",
                "maxPrice": "100000",
                "tickSize": "0.01",
            },
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": "0.01",
                "maxQty": "100",
                "stepSize": "0.01",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
            {
                "filterType": "PERCENT_PRICE",
                "multiplierUp": "1.15",
                "multiplierDown": "0.85",
                "multiplierDecimal": "4",
            },
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["ETHUSDT", "币安人生USDT"])
async def test_rules_keep_market_and_limit_lots_distinct_without_using_precision(symbol):
    mod, requests = adapter(), []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"symbols": [contract(symbol)]})

    async with mod.FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(handle)
    ) as client:
        rules = await client.rules(symbol)
        assert await client.rules(symbol) == rules
    assert rules.contract.symbol == symbol and rules.source == "binance_futures_public"
    assert rules.captured_at == NOW
    assert (rules.price_tick, rules.min_price, rules.max_price) == (
        Decimal("0.01"),
        Decimal("0.01"),
        Decimal("100000"),
    )
    assert (rules.lot_step, rules.lot_min_qty, rules.lot_max_qty) == (
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("1000"),
    )
    assert (rules.market_step, rules.market_min_qty, rules.market_max_qty) == (
        Decimal("0.01"),
        Decimal("0.01"),
        Decimal("100"),
    )
    assert rules.min_notional == 5 and rules.percent_down == Decimal("0.85")
    assert rules.percent_up == Decimal("1.15") and rules.market_take_bound == Decimal("0.3")
    assert len(requests) == 1 and requests[0].url.path == "/fapi/v1/exchangeInfo"
    assert str(requests[0].url.query) == "b''"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "spot",
        "halted",
        "wrong_margin",
        "duplicate",
        "absent",
        "missing_filter",
        "duplicate_filter",
        "zero_tick",
        "zero_market_step",
        "reversed_lot",
        "invalid_percent",
        "missing_bound",
        "float_tick",
        "bad_shape",
    ],
)
async def test_rules_reject_ambiguous_contract_or_missing_real_filters(change):
    mod, item = adapter(), contract()
    rows = [item]
    if change == "spot":
        item["contractType"] = "CURRENT_QUARTER"
    elif change == "halted":
        item["status"] = "SETTLING"
    elif change == "wrong_margin":
        item["marginAsset"] = "USDC"
    elif change == "duplicate":
        rows.append(deepcopy(item))
    elif change == "absent":
        rows = []
    elif change == "missing_filter":
        item["filters"].pop(2)
    elif change == "duplicate_filter":
        item["filters"].append(deepcopy(item["filters"][0]))
    elif change == "zero_tick":
        item["filters"][0]["tickSize"] = "0"
    elif change == "zero_market_step":
        item["filters"][2]["stepSize"] = "0"
    elif change == "reversed_lot":
        item["filters"][1]["maxQty"] = "0.0001"
    elif change == "invalid_percent":
        item["filters"][4]["multiplierDown"] = "1.1"
    elif change == "missing_bound":
        del item["marketTakeBound"]
    elif change == "float_tick":
        item["filters"][0]["tickSize"] = 0.01
    else:
        item["filters"] = {}
    async with mod.FuturesMarketClient(
        FakeClock(NOW),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"symbols": rows})),
    ) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.rules("ETHUSDT")


@pytest.mark.asyncio
async def test_expired_rules_are_refetched_and_refresh_failure_never_uses_stale_filters():
    mod, clock, count = adapter(), FakeClock(NOW), 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 2:
            return httpx.Response(503)
        item = contract()
        item["filters"][0]["tickSize"] = "0.02" if count > 2 else "0.01"
        return httpx.Response(200, json={"symbols": [item]})

    async with mod.FuturesMarketClient(clock, transport=httpx.MockTransport(handle)) as client:
        assert (await client.rules("ETHUSDT")).price_tick == Decimal("0.01")
        clock.advance_to(NOW + timedelta(seconds=300))
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.rules("ETHUSDT")
        updated = await client.rules("ETHUSDT")
        assert updated.price_tick == Decimal("0.02") and updated.captured_at == clock.utcnow()
    assert count == 3


def funding(stamp=MS - 1000, **changes):
    return {
        "symbol": "ETHUSDT",
        "fundingTime": stamp,
        "fundingRate": "0.001",
        "markPrice": "2000",
        "rateType": "Regular",
        **changes,
    }


@pytest.mark.asyncio
async def test_settled_funding_has_charge_mark_and_exclusive_cursor_not_forecast():
    mod, requests = adapter(), []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[funding()])

    async with mod.FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(handle)
    ) as client:
        result = await client.settlements("ETHUSDT", after=NOW - timedelta(hours=1), through=NOW)
    assert result.symbol == "ETHUSDT" and result.source == "binance_futures_public"
    assert result.requested_through == NOW and result.captured_at == NOW
    assert len(result.events) == 1
    event = result.events[0]
    assert event.settled_at == NOW - timedelta(seconds=1)
    assert event.rate == Decimal("0.001") and event.mark == 2000
    assert event.rate_type == "regular"
    assert requests[0].url.path == "/fapi/v1/fundingRate"
    assert dict(requests[0].url.params) == {
        "symbol": "ETHUSDT",
        "startTime": str(MS - 3599999),
        "endTime": str(MS),
        "limit": "1000",
    }


@pytest.mark.asyncio
async def test_full_funding_page_advances_by_one_millisecond_without_duplicate_charge():
    mod, starts = adapter(), []

    def handle(request):
        starts.append(int(request.url.params["startTime"]))
        if len(starts) == 1:
            rows = [funding(MS - 2000 + i) for i in range(1000)]
        else:
            rows = [funding()]
        return httpx.Response(200, json=rows)

    async with mod.FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(handle)
    ) as client:
        result = await client.settlements("ETHUSDT", after=NOW - timedelta(seconds=3), through=NOW)
    assert len(result.events) == 1001 and starts == [MS - 2999, MS - 1000]
    assert result.events[0].settled_at == NOW - timedelta(seconds=2)
    assert result.events[-1].settled_at == NOW - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_empty_funding_batch_does_not_invent_fixed_interval_charges():
    mod = adapter()
    async with mod.FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        result = await client.settlements("ETHUSDT", after=NOW - timedelta(hours=7), through=NOW)
    assert result.events == () and result.requested_through == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "special",
        "missing_type",
        "missing_mark",
        "wrong_symbol",
        "duplicate",
        "reversed",
        "before_window",
        "after_window",
        "too_many",
        "bad_shape",
    ],
)
async def test_settlement_batch_rejects_any_unsafe_or_unsupported_event_without_partial_result(
    change,
):
    mod, item = adapter(), funding()
    rows = [item]
    if change == "special":
        item["rateType"] = "Special"
    elif change == "missing_type":
        del item["rateType"]
    elif change == "missing_mark":
        del item["markPrice"]
    elif change == "wrong_symbol":
        item["symbol"] = "BTCUSDT"
    elif change == "duplicate":
        rows.append(deepcopy(item))
    elif change == "reversed":
        rows.append(funding(MS - 2000))
    elif change == "before_window":
        item["fundingTime"] = MS - 3000
    elif change == "after_window":
        item["fundingTime"] = MS + 1
    elif change == "too_many":
        rows = [funding(MS - 2000 + i) for i in range(1001)]
    else:
        rows = {"items": rows}
    async with mod.FuturesMarketClient(
        FakeClock(NOW),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=rows)),
    ) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.settlements("ETHUSDT", after=NOW - timedelta(seconds=3), through=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after,through",
    [
        (NOW - timedelta(days=32), NOW),
        (NOW, NOW - timedelta(seconds=1)),
        (NOW - timedelta(seconds=1), NOW + timedelta(milliseconds=1)),
        (NOW - timedelta(microseconds=1), NOW),
        (NOW.replace(tzinfo=None), NOW),
    ],
)
async def test_unsafe_funding_window_fails_before_network(after, through):
    def fail_http(request):
        pytest.fail("invalid funding window reached transport")

    async with adapter().FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(fail_http)
    ) as client:
        with pytest.raises(ValueError):
            await client.settlements("ETHUSDT", after=after, through=through)


@pytest.mark.asyncio
async def test_funding_capacity_limit_cannot_claim_unread_window_is_complete():
    mod, calls = adapter(), 0

    def handle(request):
        nonlocal calls
        calls += 1
        start = int(request.url.params["startTime"])
        return httpx.Response(200, json=[funding(start + i) for i in range(1000)])

    async with mod.FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.settlements("ETHUSDT", after=NOW - timedelta(seconds=10), through=NOW)
    assert calls == 4


@pytest.mark.asyncio
async def test_future_mark_at_first_reception_cannot_be_laundered_by_waiting_for_book():
    mod, clock = adapter(), FakeClock(NOW)
    mark = premium()
    mark["time"] = MS + 1000

    def handle(request):
        if request.url.path.endswith("premiumIndex"):
            return httpx.Response(200, json=mark)
        clock.advance_to(NOW + timedelta(seconds=2))
        ticker = book()
        ticker["time"] = MS + 2000
        return httpx.Response(200, json=ticker)

    async with mod.FuturesMarketClient(clock, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(mod.FuturesMarketUnavailable):
            await client.snapshot("ETHUSDT")


@pytest.mark.parametrize(
    "changes",
    [
        {"market": "spot"},
        {"mark": -1},
        {"schema_version": 9},
        {"book_at": NOW + timedelta(seconds=1)},
    ],
)
def test_snapshot_revalidates_forged_nested_quote_instance(changes):
    from agent_platform.domain.futures_market import FuturesMarketSnapshot
    from agent_platform.domain.futures_paper import FuturesPaperQuote

    quote = FuturesPaperQuote(
        symbol="ETHUSDT",
        source="binance_futures_public",
        bid="1999",
        ask="2001",
        mark="2000",
        book_at=NOW,
        mark_at=NOW,
        received_at=NOW,
    )
    with pytest.raises(ValueError):
        FuturesMarketSnapshot(
            quote=quote.model_copy(update=changes),
            index_price="2000",
            displayed_funding_rate="0.001",
            next_funding_at=NOW + timedelta(hours=1),
            bid_quantity="10",
            ask_quantity="10",
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"market": "spot"},
        {"rate_type": "special"},
        {"mark": -1},
        {"rate": 99},
        {"schema_version": 9},
    ],
)
def test_funding_window_revalidates_forged_nested_settlement_instance(changes):
    from agent_platform.domain.futures_market import FuturesFundingWindow
    from agent_platform.domain.futures_paper import FuturesPaperFunding

    event = FuturesPaperFunding(
        symbol="ETHUSDT", source="binance_futures_public", rate="0.001", mark="2000", settled_at=NOW
    )
    with pytest.raises(ValueError):
        FuturesFundingWindow(
            symbol="ETHUSDT",
            requested_after=NOW - timedelta(hours=1),
            requested_through=NOW,
            captured_at=NOW,
            events=(event.model_copy(update=changes),),
        )


@pytest.mark.asyncio
async def test_rules_revalidate_forged_nested_contract_instance():
    from agent_platform.domain.futures_market import FuturesContractRules

    async with adapter().FuturesMarketClient(
        FakeClock(NOW),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"symbols": [contract()]})
        ),
    ) as client:
        rules = await client.rules("ETHUSDT")
    bad_contract = rules.contract.model_copy(update={"market": "spot", "quote_asset": "USDC"})
    with pytest.raises(ValueError):
        FuturesContractRules.model_validate(rules.model_dump() | {"contract": bad_contract})


@pytest.mark.asyncio
async def test_rules_cache_capacity_evicts_oldest_selected_contract():
    symbols = [f"COIN{i}USDT" for i in range(33)]
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"symbols": [contract(s) for s in symbols]})

    async with adapter().FuturesMarketClient(
        FakeClock(NOW), transport=httpx.MockTransport(handle)
    ) as client:
        for symbol in symbols:
            assert (await client.rules(symbol)).contract.symbol == symbol
        assert (await client.rules(symbols[0])).contract.symbol == symbols[0]
    assert count == 34


@pytest.mark.asyncio
async def test_validated_wrapper_instances_do_not_bypass_their_own_field_validation():
    from agent_platform.domain.futures_market import FuturesFundingWindow

    async with adapter().FuturesMarketClient(
        FakeClock(NOW),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"symbols": [contract()]})
        ),
    ) as client:
        rules = await client.rules("ETHUSDT")
    async with adapter().FuturesMarketClient(FakeClock(NOW), transport=transport()) as client:
        snapshot = await client.snapshot("ETHUSDT")
    empty = FuturesFundingWindow(
        symbol="ETHUSDT",
        requested_after=NOW - timedelta(hours=1),
        requested_through=NOW,
        captured_at=NOW,
        events=(),
    )
    for value in (rules, snapshot, empty):
        with pytest.raises(ValueError):
            type(value).model_validate(value.model_copy(update={"schema_version": 9}))
