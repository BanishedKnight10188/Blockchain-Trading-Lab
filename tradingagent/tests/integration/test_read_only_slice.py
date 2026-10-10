"""Fake market/account to real SQLite and Web, with no real endpoint access."""

import asyncio
import importlib

from fastapi.testclient import TestClient

from agent_platform.adapters.fake.account import FakeAccount
from agent_platform.adapters.fake.clock import FakeClock
from agent_platform.adapters.fake.market import FakeMarket
from agent_platform.adapters.sqlite import open_store
from agent_platform.adapters.sqlite.sessions import SqliteSessionStore
from agent_platform.application.account_sync import AccountSyncService
from agent_platform.application.sessions import SessionService
from tests.application.test_queries import NOW, report
from tests.runtime.test_read_only import observation
from tests.web.test_session_page import open_page


def test_fake_read_only_slice_preserves_confirmed_style_and_exact_balance(tmp_path):
    bootstrap = importlib.import_module("agent_platform.bootstrap")
    latest = importlib.import_module("agent_platform.runtime.latest")
    queries = importlib.import_module("agent_platform.application.queries")
    runtime_module = importlib.import_module("agent_platform.runtime.read_only")
    path = tmp_path / "fake-slice.sqlite3"
    clock = FakeClock(NOW)

    async def build():
        store = await open_store(path)
        cache = latest.LatestOverview(
            clock, mode="fake", market_source="fake", account_source="fake"
        )
        market = FakeMarket()
        market.observe(observation())
        sync = AccountSyncService(FakeAccount(report().account), store, clock)
        runtime = runtime_module.ReadOnlyRuntime(
            cache, clock, market=market, account_sync=sync, account_ref=report().account.account_ref
        )
        return bootstrap.ApplicationServices(
            SessionService(SqliteSessionStore(path), clock),
            queries.QueryService(cache, clock),
            runtime,
        )

    services = asyncio.run(build())
    app = importlib.import_module("agent_platform.web.app").create_app(path, services=services)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = open_page(client)

        async def background_ready():
            async with asyncio.timeout(3):
                frame = await services.runtime.cache.latest()
                while frame.sync is None:
                    frame = await services.runtime.cache.wait(frame.event_id)

        client.portal.call(background_ready)
        created = client.post(
            "/api/sessions", headers=headers, json={"style_strength": 73, "style_confirmed": True}
        )
        assert created.status_code == 201
        data = client.get("/api/overview").json()
        assert data["mode"] == "fake" and data["market"]["source"] == "fake"
        assert data["market"]["bid"] == "60000"
        assert data["account"]["status"] == "fresh"
        assert data["account"]["quantity"] == "0.10000001"
        assert data["account"]["average_cost"] is None
        assert "private-account" not in client.get("/api/overview").text
        assert client.get("/api/session").json()["session"]["style"]["strength"] == 73
        assert services.runtime.running
    assert not services.runtime.running
