"""Browser queries never start Binance requests or expose private identifiers."""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from tests.web.test_session_page import open_page


def factory():
    return importlib.import_module("agent_platform.web.app").create_app


def test_default_overview_is_disabled_and_authenticated(tmp_path):
    with TestClient(factory()(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        assert client.get("/api/overview").status_code == 403
        assert client.get("/api/events").status_code == 403
        page = client.get("/overview")
        assert page.status_code == 200 and "BTCUSDT" in page.text
        data = client.get("/api/overview").json()
        assert data["mode"] == "disabled"
        assert data["market"]["status"] == "not_connected"
        assert data["market"]["price"] is None and data["account"]["quantity"] is None
        assert data["advice_status"] == "unavailable" and data["jev_status"] == "unspecified"
        assert "api_secret" not in client.get("/api/overview").text


def test_only_local_static_resources_and_no_financial_write_routes(tmp_path):
    app = factory()(tmp_path / "web.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        open_page(client)
        page = client.get("/overview")
        assert 'src="https://' not in page.text and 'href="https://' not in page.text
        assert client.get("/static/overview.js").status_code == 200
        assert "connect-src 'self'" in page.headers["content-security-policy"]
        paths = {route.path for route in app.routes}
        assert not any(
            word in path for path in paths for word in ("/order", "/transfer", "/withdraw")
        )
        for path in ("/api/overview", "/api/events"):
            assert client.post(path, json={}).status_code == 405


def test_events_route_has_latest_only_headers_and_no_sensitive_error_reflection(tmp_path):
    from starlette.requests import Request

    app = factory()(tmp_path / "web.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        open_page(client)
        assert client.get("/api/overview", headers={"Host": "foreign.example"}).status_code == 400
        route = next(route for route in app.routes if route.path == "/api/events")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/events",
                "headers": [(b"last-event-id", b"private-secret-must-not-reflect")],
                "app": app,
            }
        )
        response = asyncio.run(route.endpoint(request))
        assert response.media_type == "text/event-stream"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_injected_startup_cancellation_cleans_new_runtime_tasks(tmp_path):
    from agent_platform.adapters.fake.clock import FakeClock
    from agent_platform.application.queries import QueryService
    from agent_platform.bootstrap import ApplicationServices
    from agent_platform.runtime.latest import LatestOverview
    from agent_platform.runtime.read_only import ReadOnlyRuntime
    from tests.application.test_queries import NOW

    clock = FakeClock(NOW)
    cache = LatestOverview(clock)
    runtime = ReadOnlyRuntime(cache, clock)
    services = ApplicationServices(None, QueryService(cache, clock), runtime)
    app = factory()(tmp_path / "cancel.sqlite3", services=services)
    lifespan = app.router.lifespan_context(app)
    entering = asyncio.create_task(lifespan.__aenter__())
    await asyncio.sleep(0)
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    try:
        assert not runtime.running
    finally:
        await runtime.stop()
