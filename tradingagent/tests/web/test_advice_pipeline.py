"""Local session controls and Fake-to-SQLite advice use the same browser guards."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_platform.application.queries import QueryService
from agent_platform.application.sessions import SessionService
from agent_platform.bootstrap import ApplicationServices
from agent_platform.web.app import create_app
from tests.adapters.test_sqlite_decisions import context as _context
from tests.application.test_decision_service import Rules
from tests.runtime.test_decisions import idle, runtime
from tests.web.test_session_page import open_page

context = _context


def test_local_lifecycle_requires_browser_guard_strict_revision_and_valid_transition(tmp_path):
    with TestClient(create_app(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        headers = open_page(client)
        created = client.post(
            "/api/sessions",
            headers=headers,
            json={
                "style_strength": 37,
                "style_confirmed": True,
            },
        ).json()["session"]
        path = f"/api/sessions/{created['session_id']}/state"
        body = {"status": "running", "expected_revision": 1}
        assert client.put(path, json=body).status_code == 403
        assert (
            client.put(
                path, json=body, headers={**headers, "Origin": "https://foreign.example"}
            ).status_code
            == 403
        )
        assert (
            client.put(path, json={**body, "expected_revision": True}, headers=headers).status_code
            == 422
        )
        assert (
            client.put(path, json={**body, "status": "paused"}, headers=headers).status_code == 422
        )
        started = client.put(path, json=body, headers=headers)
        assert started.status_code == 200 and started.json()["session"]["status"] == "running"
        assert started.json()["session"]["style"]["strength"] == 37
        assert started.json()["session"]["style_revision"] == 1
        assert client.put(path, json=body, headers=headers).status_code == 409
        assert (
            client.put(
                path, json={"status": "paused", "expected_revision": 2}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.put(
                path, json={"status": "closed", "expected_revision": 3}, headers=headers
            ).status_code
            == 200
        )
        assert client.get("/api/session").json()["session"] is None


def test_default_assembled_pipeline_reports_missing_facts_and_no_paid_calls(tmp_path):
    with TestClient(create_app(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        open_page(client)
        view = client.get("/api/overview").json()
        assert view["advice"]["status"] == "unavailable"
        assert view["advice"]["reasons"] == ["no_session"]
        assert view["decision_runtime"]["running"]
        assert not view["paid_models_enabled"]
        page = client.get("/overview").text
        assert 'id="advice-state"' in page and 'id="hard-alert"' in page
        assert "建议发布流程尚未接入" not in page


@pytest.mark.asyncio
async def test_fake_sqlite_runtime_to_web_advice_and_pause(context):
    worker, advice, _, cache, clock, _ = await runtime(context, rules=Rules())
    query = QueryService(cache, clock, advice=advice, decision_runtime=worker)
    services = ApplicationServices(SessionService(context[2], clock), query, worker)
    app = create_app(context[0], services=services)
    async with app.router.lifespan_context(app):
        await worker.tick()
        await idle(worker)
        assert (await query.overview()).advice_status == "published"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            page = await client.get("/overview")
            token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
            headers = {"Origin": "http://127.0.0.1", "X-CSRF-Token": token}
            response = await client.get("/api/overview")
            data = response.json()
            assert data["mode"] == "fake" and data["advice"]["action"] == "hold"
            assert data["advice"]["source"] == "rule" and data["advice"]["style_strength"] == 67
            assert "local-spot" not in response.text
            paused = await client.put(
                "/api/sessions/session-1/state",
                headers=headers,
                json={
                    "status": "paused",
                    "expected_revision": 2,
                },
            )
            assert paused.status_code == 200
            changed = (await client.get("/api/overview")).json()
            assert changed["advice_status"] == "superseded" and changed["advice"]["action"] is None
    assert not worker.running
