import re

import pytest
from fastapi.testclient import TestClient

from agent_platform.web.app import create_app


def open_page(client):
    page = client.get("/agent")
    assert page.status_code == 200
    token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    return {"Origin": "http://127.0.0.1", "X-CSRF-Token": token}


def payload(**updates):
    return dict(
        mode="auto",
        jev_enabled=True,
        trader_enabled=True,
        confirmed=True,
        expected_revision=0,
        **updates,
    )


def test_page_and_settings_are_protected_and_auto_is_not_executable(tmp_path):
    with TestClient(create_app(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        assert client.get("/api/controls").status_code == 403
        headers = open_page(client)
        page = client.get("/agent").text
        assert "Haiku 常态主分析" in page and "可选 JEV 建议" in page
        assert "自动操盘" in client.get("/jev-trader").text
        assert 'src="/static/jev-advice.js"' in page
        assert client.get("/static/jev-advice.js").status_code == 200
        assert client.post("/api/controls", json=payload()).status_code == 403
        assert (
            client.post(
                "/api/controls",
                headers={**headers, "Origin": "https://foreign.example"},
                json=payload(),
            ).status_code
            == 403
        )
        initial = client.get("/api/controls").json()
        assert initial["revision"] == 0 and initial["operation"]["mode"] == "advisory"
        saved = client.post("/api/controls", headers=headers, json=payload())
        assert saved.status_code == 200
        value = saved.json()
        assert value["operation"]["mode"] == "auto"
        assert value["operation"]["writes_enabled"] is False
        assert value["operation"]["state"] == "execution_unconfigured"
        assert value["jev"]["enabled"] is True
        assert value["jev"]["connection_state"] == "not_connected"
        assert value["jev"]["decision"] is None
        status = client.get("/api/status").json()
        assert status["operation"] == value["operation"]
        assert status["model_modules"]["jev"]["enabled"] is True
        assert status["paid_models_enabled"] is False and status["budget"]["hourly_call_count"] == 0
        assert client.post("/api/controls", headers=headers, json=payload()).status_code == 409


def test_setting_survives_new_application_and_switching_off_pauses_execution(tmp_path):
    path = tmp_path / "recover.sqlite3"
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client:
        headers = open_page(client)
        assert client.post("/api/controls", headers=headers, json=payload()).status_code == 200
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client:
        headers = open_page(client)
        current = client.get("/api/controls").json()
        assert current["revision"] == 1 and current["operation"]["mode"] == "auto"
        changed = client.post(
            "/api/controls",
            headers=headers,
            json={**payload(), "expected_revision": 1, "trader_enabled": False},
        )
        assert changed.status_code == 200
        assert changed.json()["operation"]["state"] == "trader_disabled"
        assert changed.json()["jev_advice"]["enabled"] is True
        assert changed.json()["revision"] == 2
        assert (
            client.get("/api/status").json()["model_modules"]["analysis_model"]
            == "anthropic/claude-haiku-5.5"
        )


def test_conflicting_source_is_a_safe_503_and_does_not_export_auto_as_ready(tmp_path):
    with TestClient(create_app(tmp_path / "shared.sqlite3"), base_url="http://127.0.0.1") as client:
        headers = open_page(client)
        assert client.post("/api/controls", headers=headers, json=payload()).status_code == 200
        # Bind this reader to the same source invariant as a running production instance.
        client.app.state.services.controls.production_reads = True
        for path in ("/api/controls", "/api/status"):
            response = client.get(path)
            assert response.status_code == 503
            assert "模式" in response.json()["detail"]
            assert "testnet auto mode cannot" not in response.text


@pytest.mark.parametrize(
    "invalid",
    [
        {"confirmed": False},
        {"confirmed": 1},
        {"jev_enabled": "true"},
        {"mode": "live"},
        {"expected_revision": True},
        {"expected_revision": -1},
        {"expected_revision": "0"},
        {"api_key": "private-test-key"},
        {"execution_environment": "production"},
    ],
)
def test_invalid_controls_are_not_saved_and_do_not_echo_input(tmp_path, invalid):
    with TestClient(create_app(tmp_path / "web.sqlite3"), base_url="http://127.0.0.1") as client:
        headers = open_page(client)
        response = client.post("/api/controls", headers=headers, json={**payload(), **invalid})
        assert response.status_code == 422
        assert "private-test-key" not in response.text
        assert client.get("/api/controls").json()["revision"] == 0
