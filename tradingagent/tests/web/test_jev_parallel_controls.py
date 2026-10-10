"""Haiku, optional Jev advice and dedicated Jev trading are separate modules."""

import importlib
import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agent_platform.domain.model_modules import ModelModulesConfig
from agent_platform.web.app import create_app


def headers(client):
    page = client.get("/overview")
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return {"Origin": "http://127.0.0.1", "X-CSRF-Token": token}


def test_module_defaults_and_enablement_are_separate():
    module = importlib.import_module("agent_platform.domain.model_modules")
    assert module.JevTraderSettings().enabled is False
    default = ModelModulesConfig()
    assert default.analysis_mode == "continuous"
    assert not default.jev.enabled and not default.jev_trader.enabled
    for advice, trader in ((False, False), (False, True), (True, False), (True, True)):
        value = ModelModulesConfig(jev={"enabled": advice}, jev_trader={"enabled": trader})
        assert value.jev.enabled is advice and value.jev_trader.enabled is trader
        assert value.analysis_mode == "continuous"


def test_advice_and_trader_pages_are_distinct_and_scoped(tmp_path):
    with TestClient(
        create_app(tmp_path / "separate.sqlite3"), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        advice_page = client.get("/overview")
        trader_page = client.get("/jev-trader")
        assert trader_page.status_code == 200
        assert 'id="advice-form"' in advice_page.text
        assert 'id="trader-form"' not in advice_page.text
        assert 'id="trader-form"' in trader_page.text
        assert "Haiku 常态主分析" in advice_page.text
        assert 'id="last-price"' in advice_page.text and 'id="quote-chart"' in advice_page.text
        assert client.post("/api/controls/advice", json={}).status_code == 403
        assert client.post("/api/controls/trader", json={}).status_code == 403
        first = client.post(
            "/api/controls/trader",
            headers=auth,
            json={
                "mode": "auto",
                "enabled": True,
                "confirmed": True,
                "expected_revision": 0,
            },
        )
        assert first.status_code == 200
        state = first.json()
        assert not state["jev_advice"]["enabled"] and state["jev_trader"]["enabled"]
        assert state["operation"]["state"] == "execution_unconfigured"
        trader_revision = state["jev_trader"]["revision"]
        second = client.post(
            "/api/controls/advice",
            headers=auth,
            json={
                "enabled": True,
                "confirmed": True,
                "expected_revision": state["revision"],
            },
        )
        assert second.status_code == 200
        state = second.json()
        assert state["jev_advice"]["enabled"] and state["jev_trader"]["enabled"]
        assert state["jev_trader"]["revision"] == trader_revision
        third = client.post(
            "/api/controls/advice",
            headers=auth,
            json={
                "enabled": False,
                "confirmed": True,
                "expected_revision": state["revision"],
            },
        )
        assert third.status_code == 200
        state = third.json()
        assert state["operation"]["state"] == "execution_unconfigured"
        assert state["jev_trader"]["revision"] == trader_revision
        advice_revision = state["jev_advice"]["revision"]
        fourth = client.post(
            "/api/controls/trader",
            headers=auth,
            json={
                "mode": "auto",
                "enabled": False,
                "confirmed": True,
                "expected_revision": state["revision"],
            },
        )
        assert fourth.status_code == 200
        state = fourth.json()
        assert state["operation"]["state"] == "trader_disabled"
        assert state["jev_advice"]["revision"] == advice_revision
        status = client.get("/api/status").json()
        assert status["model_modules"]["analysis_mode"] == "continuous"
        assert not status["paid_models_enabled"] and status["budget"]["hourly_call_count"] == 0
        assert state["operation"]["writes_enabled"] is False
        fifth = client.post(
            "/api/controls/advice",
            headers=auth,
            json={
                "enabled": True,
                "confirmed": True,
                "expected_revision": state["revision"],
            },
        )
        assert fifth.status_code == 200
        state = fifth.json()
        advice_revision = state["jev_advice"]["revision"]
        sixth = client.post(
            "/api/controls/trader",
            headers=auth,
            json={
                "mode": "auto",
                "enabled": True,
                "confirmed": True,
                "expected_revision": state["revision"],
            },
        )
        assert sixth.status_code == 200
        state = sixth.json()
        assert state["jev_advice"]["enabled"] is True
        assert state["jev_advice"]["revision"] == advice_revision
        assert state["jev_trader"]["enabled"] is True
        seventh = client.post(
            "/api/controls/trader",
            headers=auth,
            json={
                "mode": "auto",
                "enabled": False,
                "confirmed": True,
                "expected_revision": state["revision"],
            },
        )
        assert seventh.status_code == 200
        assert seventh.json()["jev_advice"]["enabled"] is True
        assert seventh.json()["jev_advice"]["revision"] == advice_revision


def test_old_jev_setting_cannot_silently_enable_trading_module():
    domain = importlib.import_module("agent_platform.domain.agent_controls")
    old = domain.AgentControlState(
        revision=2,
        updated_at=datetime(2026, 10, 6, tzinfo=UTC),
        operation={"mode": "auto"},
        jev={"enabled": True},
    )
    assert old.jev.enabled and not old.trader.enabled
    assert (
        old.operation.public_state(trader_enabled=old.trader.enabled)["state"] == "trader_disabled"
    )


def test_old_all_settings_request_must_not_implicitly_disable_trader(tmp_path):
    with TestClient(
        create_app(tmp_path / "legacy-request.sqlite3"), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        saved = client.post(
            "/api/controls/trader",
            headers=auth,
            json={"mode": "auto", "enabled": True, "confirmed": True, "expected_revision": 0},
        )
        assert saved.status_code == 200
        before = saved.json()
        ambiguous = client.post(
            "/api/controls",
            headers=auth,
            json={
                "mode": "auto",
                "jev_enabled": False,
                "confirmed": True,
                "expected_revision": before["revision"],
            },
        )
        assert ambiguous.status_code == 422
        assert client.get("/api/controls").json() == before
