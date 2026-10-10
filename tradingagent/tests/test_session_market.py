"""All eligible contracts, exact closed history and immutable session identity."""

import importlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.domain.sessions import AgentSession, TradingStyle

NOW = datetime(2026, 10, 7, 8, 30, tzinfo=UTC)


def domain():
    return importlib.import_module("agent_platform.domain.session_market")


def adapter():
    return importlib.import_module("agent_platform.adapters.binance_direct.futures_public")


def contract(symbol="ETHUSDT", **changes):
    return {
        "symbol": symbol,
        "baseAsset": symbol.removesuffix("USDT"),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        **changes,
    }


def row(start):
    ms = int(start.timestamp() * 1000)
    return [
        ms,
        "2000",
        "2010",
        "1990",
        "2001",
        "100",
        ms + 3599999,
        "200000",
        12,
        "60",
        "120000",
        "0",
    ]


def test_legacy_session_retains_spot_and_target_survives_style_change():
    d = domain()
    old = AgentSession(
        session_id="legacy", style=TradingStyle(strength=80), created_at=NOW, updated_at=NOW
    )
    assert old.analysis_target.market == "spot"
    assert old.analysis_target.symbol == "BTCUSDT"
    selected = AgentSession(
        **{
            **old.model_dump(),
            "analysis_target": d.SessionAnalysisTarget(
                market="usdt_perpetual", symbol="ETHUSDT", history_days=7
            ),
        }
    )
    assert (
        selected.change_style(TradingStyle(strength=70), NOW).analysis_target
        == selected.analysis_target
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ethusdt"},
        {"symbol": "ETHUSD"},
        {"history_days": 2},
        {"history_days": True},
        {"interval": "1m"},
        {"market": "spot", "symbol": "ETHUSDT"},
    ],
)
def test_target_rejects_unsupported_or_ambiguous_identity(changes):
    with pytest.raises(ValidationError):
        domain().SessionAnalysisTarget(
            **{"market": "usdt_perpetual", "symbol": "ETHUSDT", **changes}
        )


@pytest.mark.asyncio
async def test_catalog_contains_all_eligible_usdt_perpetuals_and_is_cached():
    rows = [
        contract("BTCUSDT"),
        contract(),
        contract("1000SHIBUSDT"),
        contract("币安人生USDT"),
        contract("SOLUSDT", status="SETTLING"),
        contract("ADAUSDT", contractType="CURRENT_QUARTER"),
        contract("ETHUSDC", quoteAsset="USDC", marginAsset="USDC"),
    ]
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET" and "authorization" not in request.headers
        return httpx.Response(200, json={"symbols": rows})

    async with adapter().FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        assert {x.symbol for x in await client.catalog()} == {
            "BTCUSDT",
            "ETHUSDT",
            "1000SHIBUSDT",
            "币安人生USDT",
        }
        assert await client.catalog()
    assert paths == ["/fapi/v1/exchangeInfo"]


@pytest.mark.asyncio
@pytest.mark.parametrize("days,count", [(1, 24), (7, 168), (30, 720)])
async def test_history_complete_closed_window_and_bounded_pagination(days, count):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract()]})
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": int(NOW.timestamp() * 1000)})
        assert request.url.host == "fapi.binance.com"
        q = request.url.params
        assert q["symbol"] == "ETHUSDT" and q["interval"] == "1h"
        first = datetime.fromtimestamp(int(q["startTime"]) / 1000, UTC)
        limit = int(q["limit"])
        assert limit <= 499
        return httpx.Response(200, json=[row(first + timedelta(hours=i)) for i in range(limit)])

    async with adapter().FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.history(
            domain().SessionAnalysisTarget(
                market="usdt_perpetual", symbol="ETHUSDT", history_days=days
            )
        )
    assert len(result.candles) == count
    assert result.candles[-1].closed_at == NOW.replace(minute=0) - timedelta(milliseconds=1)
    assert result.candles[0].opened_at == NOW.replace(minute=0) - timedelta(days=days)
    assert result.contract.symbol == "ETHUSDT" and result.source == "binance_futures_public"
    assert len(result.content_hash) == 64
    assert sum(r.url.path.endswith("klines") for r in calls) == (2 if days == 30 else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["gap", "duplicate", "future", "wrong_close", "ohlc", "empty"])
async def test_bad_history_never_becomes_analysis_evidence(failure):
    def handler(request):
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [contract()]})
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": int(NOW.timestamp() * 1000)})
        first = datetime.fromtimestamp(int(request.url.params["startTime"]) / 1000, UTC)
        rows = [row(first + timedelta(hours=i)) for i in range(24)]
        if failure == "gap":
            rows.pop(5)
        if failure == "duplicate":
            rows[5] = rows[4]
        if failure == "future":
            rows[-1] = row(NOW.replace(minute=0))
        if failure == "wrong_close":
            rows[0][6] += 1
        if failure == "ohlc":
            rows[0][2] = "1"
        if failure == "empty":
            rows = []
        return httpx.Response(200, json=rows)

    async with adapter().FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(adapter().FuturesPublicError):
            await client.history(
                domain().SessionAnalysisTarget(
                    market="usdt_perpetual", symbol="ETHUSDT", history_days=1
                )
            )


@pytest.mark.asyncio
async def test_rate_limit_blocks_followups_without_retry_or_private_access():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    async with adapter().FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        for _ in range(2):
            with pytest.raises(adapter().FuturesPublicError):
                await client.catalog()
        with pytest.raises(ValueError):
            await client.get("/fapi/v1/order", {})
    assert len(calls) == 1


def test_futures_proxy_only_accepts_credential_free_loopback_http():
    from agent_platform.config import RuntimeConfig

    assert RuntimeConfig(futures_proxy="http://127.0.0.1:7897").futures_proxy.endswith(":7897")
    for value in (
        "http://outside.example:7897",
        "http://user:secret@127.0.0.1:7897",
        "http://127.0.0.1:0",
        "http://127.0.0.1:7897/path",
    ):
        with pytest.raises(ValueError):
            RuntimeConfig(futures_proxy=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["duplicate_json", "oversized", "compressed"])
async def test_untrusted_response_bounds_fail_before_catalog_use(kind):
    def handler(request):
        if kind == "duplicate_json":
            return httpx.Response(200, content=b'{"symbols":[],"symbols":[]}')
        if kind == "oversized":
            return httpx.Response(200, content=b" " * (4 * 1024 * 1024 + 1))
        return httpx.Response(
            200, json={"symbols": [contract()]}, headers={"Content-Encoding": "br"}
        )

    async with adapter().FuturesPublicClient(
        FakeClock(NOW), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(adapter().FuturesPublicError):
            await client.catalog()
