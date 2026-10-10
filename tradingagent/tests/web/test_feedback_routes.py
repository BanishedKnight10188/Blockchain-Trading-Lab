"""Fake-to-SQLite local records use browser guards and keep account scope server-side."""

import asyncio
import importlib
import re
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_platform.application.attribution import AttributionService
from agent_platform.application.feedback import FeedbackService
from agent_platform.application.queries import QueryService
from agent_platform.application.reports import ReportService
from agent_platform.application.sessions import SessionService
from agent_platform.bootstrap import ApplicationServices
from agent_platform.web.app import create_app
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_advice_queries import assembly
from tests.application.test_attribution import setup
from tests.domain.test_decisions import NOW
from tests.web.test_session_page import open_page

context = _context


class Runtime:
    async def start(self):
        pass

    async def stop(self):
        pass


@asynccontextmanager
async def browser(context):
    await setup(context)
    factory, advice, _, cache, clock = await assembly(context)
    adapter = importlib.import_module("agent_platform.adapters.sqlite.reports")
    queries = importlib.import_module("agent_platform.application.ledger_queries")
    store = adapter.SqliteReportStore(context[0], clock=clock)
    services = ApplicationServices(
        SessionService(context[2], clock),
        QueryService(cache, clock, advice=advice),
        Runtime(),
        feedback=FeedbackService(
            store=store, states=store, snapshots=factory, clock=clock, account_ref="local-spot"
        ),
        attribution=AttributionService(store=store, clock=clock, account_ref="local-spot"),
        reports=ReportService(store=store, clock=clock, account_ref="local-spot"),
        ledger=queries.LedgerQueryService(store=store, source=cache, account_ref="local-spot"),
    )
    app = create_app(context[0], services=services)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        page = await client.get("/")
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": token}
        client.record_store = store
        yield client, headers, clock


def feedback_body():
    return dict(
        feedback_id="web-feedback",
        kind="accepted",
        recommendation_id="advice-1",
        explanation="用户确认采纳测试观点",
        confirmed=True,
    )


def attribution_body():
    return dict(
        operation_id="web-attribution",
        trade_id="trade-1",
        recommendation_id="advice-1",
        expected_revision=1,
        explanation="用户明确确认来源",
        confirmed=True,
    )


def report_body():
    return dict(
        report_id="web-report",
        side="buy",
        price="60000",
        quantity="0.01",
        executed_at=NOW.isoformat(),
        exchange_trade_id="trade-1",
        confirmed=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/feedback", feedback_body()),
        ("/api/attributions", attribution_body()),
        ("/api/reports", report_body()),
    ],
)
async def test_local_record_writes_require_cookie_origin_and_explicit_confirmation(
    context, path, body
):
    async with browser(context) as (client, headers, _):
        assert (await client.post(path, json=body)).status_code == 403
        assert (
            await client.post(
                path, headers={**headers, "Origin": "https://foreign.example"}, json=body
            )
        ).status_code == 403
        assert (
            await client.post(path, headers=headers, json={**body, "confirmed": False})
        ).status_code == 422
        assert (
            await client.post(path, headers=headers, json={**body, "confirmed": 1})
        ).status_code == 422
        assert (
            await client.post(path, headers=headers, json={**body, "account_ref": "another"})
        ).status_code == 422


@pytest.mark.asyncio
async def test_feedback_http_retry_keeps_first_record_time_and_terminal_advice(context):
    async with browser(context) as (client, headers, clock):
        body = feedback_body()
        first = await client.post("/api/feedback", json=body, headers=headers)
        assert first.status_code == 201
        clock.advance_to(NOW + timedelta(seconds=2))
        repeated = await client.post("/api/feedback", json=body, headers=headers)
        assert repeated.status_code == 201 and repeated.json() == first.json()
        assert "local-spot" not in first.text
        assert (
            await client.post(
                "/api/feedback", headers=headers, json={**body, "explanation": "不同说明"}
            )
        ).status_code == 409
        overview = (await client.get("/api/overview")).json()
        assert overview["advice"]["status"] == "accepted" and overview["advice"]["action"] is None


@pytest.mark.asyncio
async def test_trade_attribution_manual_report_and_verification_are_independent_queries(context):
    async with browser(context) as (client, headers, clock):
        body = attribution_body()
        first = await client.post("/api/attributions", json=body, headers=headers)
        assert first.status_code == 201 and first.json()["revision"] == 2
        clock.advance_to(NOW + timedelta(seconds=1))
        assert (
            await client.post("/api/attributions", json=body, headers=headers)
        ).json() == first.json()
        corrected = await client.post(
            "/api/attributions",
            headers=headers,
            json={
                **body,
                "operation_id": "correction-2",
                "recommendation_id": None,
                "expected_revision": 2,
            },
        )
        assert corrected.status_code == 201 and corrected.json()["revision"] == 3
        report = await client.post("/api/reports", headers=headers, json=report_body())
        assert report.status_code == 201 and report.json()["status"] == "pending"
        verification = dict(operation_id="verification-1", expected_revision=1, confirmed=True)
        checked = await client.post(
            "/api/reports/web-report/verify", headers=headers, json=verification
        )
        assert checked.status_code == 200 and checked.json()["status"] == "verified"
        assert checked.json()["source"] == "user_reported"
        assert (
            await client.post(
                "/api/reports/web-report/verify",
                headers=headers,
                json={**verification, "expected_revision": True},
            )
        ).status_code == 422
        records = await client.get("/api/trading-records")
        assert records.status_code == 200 and "local-spot" not in records.text
        data = records.json()
        assert data["mode"] == "fake"
        assert data["trades"][0]["attribution"]["original_author"] == "human"
        assert data["trades"][0]["price"] == "60000"
        assert (
            data["reports"][0]["source"] == "user_reported"
            and data["reports"][0]["status"] == "verified"
        )


def test_default_bootstrap_can_record_independent_human_idea_without_network(tmp_path):
    with TestClient(create_app(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        assert client.get("/api/trading-records").status_code == 403
        headers = open_page(client)
        body = dict(
            feedback_id="independent-idea",
            kind="independent",
            recommendation_id=None,
            explanation="我想继续观察",
            confirmed=True,
            modified_assessment={"action": "hold", "explanation": "我自己选择观察"},
        )
        response = client.post("/api/feedback", headers=headers, json=body)
        assert response.status_code == 201
        records = client.get("/api/trading-records")
        assert records.json()["mode"] == "disabled"
        assert records.json()["feedback"][0]["final_decision_maker"] == "human"
        assert "binance-local" not in records.text
        assert not client.get("/api/overview").json()["paid_models_enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,body,lookup",
    [
        ("/api/feedback", feedback_body(), "feedback"),
        ("/api/attributions", attribution_body(), "attribution_change"),
        ("/api/reports", report_body(), "report"),
    ],
)
async def test_concurrent_identical_http_retry_reuses_server_owned_timestamp(
    context, monkeypatch, path, body, lookup
):
    async with browser(context) as (client, headers, clock):
        reads, ticks = 0, 0
        barrier = asyncio.Event()
        original = getattr(client.record_store, lookup)

        async def simultaneous_read(*args, **kwargs):
            nonlocal reads
            found = await original(*args, **kwargs)
            reads += 1
            if reads <= 2:
                if reads == 2:
                    barrier.set()
                await barrier.wait()
            return found

        def advancing_time():
            nonlocal ticks
            ticks += 1
            return NOW + timedelta(milliseconds=ticks)

        monkeypatch.setattr(client.record_store, lookup, simultaneous_read)
        monkeypatch.setattr(clock, "utcnow", advancing_time)
        responses = await asyncio.gather(
            *(client.post(path, headers=headers, json=body) for _ in range(2))
        )
        assert [response.status_code for response in responses] == [201, 201]
        assert responses[0].json() == responses[1].json()


@pytest.mark.asyncio
async def test_record_query_is_bounded_and_cursor_does_not_repeat_trades(context):
    async with browser(context) as (client, _, _):
        first = (await client.get("/api/trading-records?limit=1")).json()
        assert len(first["trades"]) == 1 and first["record_limit"] == 1
        next_page = (
            await client.get(
                f"/api/trading-records?after_sequence={first['next_trade_sequence']}&limit=1"
            )
        ).json()
        assert next_page["trades"] == []
        for invalid in (
            "limit=0",
            "limit=51",
            "after_sequence=-1",
            "after_sequence=9223372036854775808",
        ):
            assert (await client.get("/api/trading-records?" + invalid)).status_code == 422
        with pytest.raises(ValueError):
            await client.record_store.ledger("local-spot", after_sequence=9223372036854775808)


@pytest.mark.asyncio
async def test_record_pages_cover_large_import_and_filter_other_account_and_symbol(context):
    from agent_platform.domain.account import TradeBatch
    from agent_platform.domain.events import JournalEvent

    async with browser(context) as (client, _, _):
        original = (await context[1].observed_trades("local-spot", "BTCUSDT"))[0]
        many = tuple(
            original.model_copy(update={"trade_id": f"trade-{index}"}) for index in range(2, 63)
        )
        batch = TradeBatch(account_ref="local-spot", symbol="BTCUSDT", trades=many)
        await context[1].ingest(
            batch,
            context[-1],
            JournalEvent(
                event_id="bulk-page-import",
                aggregate_id="local-spot",
                kind="trades_imported",
                payload=batch,
                occurred_at=NOW,
            ),
        )
        for account_ref, symbol, identity in (
            ("another", "BTCUSDT", "foreign-trade"),
            ("local-spot", "ETHUSDT", "foreign-symbol"),
        ):
            trade = original.model_copy(
                update={"account_ref": account_ref, "symbol": symbol, "trade_id": identity}
            )
            batch = TradeBatch(account_ref=account_ref, symbol=symbol, trades=(trade,))
            await context[1].ingest(
                batch,
                context[-1].model_copy(update={"account_ref": account_ref}),
                JournalEvent(
                    event_id=identity,
                    aggregate_id=account_ref,
                    kind="trades_imported",
                    payload=batch,
                    occurred_at=NOW,
                ),
            )
        first = (await client.get("/api/trading-records")).json()
        assert len(first["trades"]) == 50 and first["has_more_trades"]
        second = (
            await client.get(f"/api/trading-records?after_sequence={first['next_trade_sequence']}")
        ).json()
        ids = [item["trade_id"] for item in first["trades"] + second["trades"]]
        assert len(ids) == len(set(ids)) == 62
        assert not second["has_more_trades"]
        assert "foreign-trade" not in ids and "foreign-symbol" not in ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"quantity": 0.01},
        {"quantity": "1e129"},
        {"executed_at": "2026-10-05T00:00:00"},
        {"source": "binance"},
    ],
)
async def test_manual_report_http_rejects_inexact_amounts_naive_time_and_source_override(
    context, updates
):
    async with browser(context) as (client, headers, _):
        response = await client.post(
            "/api/reports", headers=headers, json={**report_body(), **updates}
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_http_retry_recovers_when_first_feedback_commits_after_second_service_lookup(
    context, monkeypatch
):
    async with browser(context) as (client, headers, clock):
        calls, ticks = 0, 0
        second_read, committed = asyncio.Event(), asyncio.Event()
        read, write = client.record_store.feedback, client.record_store.record_feedback

        async def gated_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            index = calls
            found = await read(*args, **kwargs)
            if index == 3:
                await second_read.wait()
            elif index == 4:
                second_read.set()
                await committed.wait()
            return found

        async def commit_then_release(*args, **kwargs):
            receipt = await write(*args, **kwargs)
            committed.set()
            return receipt

        def advancing_time():
            nonlocal ticks
            ticks += 1
            return NOW + timedelta(milliseconds=ticks)

        monkeypatch.setattr(client.record_store, "feedback", gated_read)
        monkeypatch.setattr(client.record_store, "record_feedback", commit_then_release)
        monkeypatch.setattr(clock, "utcnow", advancing_time)
        async with asyncio.timeout(3):
            responses = await asyncio.gather(
                *(
                    client.post("/api/feedback", headers=headers, json=feedback_body())
                    for _ in range(2)
                )
            )
        assert [response.status_code for response in responses] == [201, 201]
        assert responses[0].json() == responses[1].json()
