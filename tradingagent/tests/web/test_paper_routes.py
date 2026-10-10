"""Actual protected ASGI controls for the local virtual wallet."""

import pytest
from fastapi.testclient import TestClient

from agent_platform.config import RuntimeConfig
from agent_platform.web.app import create_app
from tests.domain.test_paper_trading import settings_data
from tests.web.test_jev_parallel_controls import headers


def configured_app(path):
    return create_app(path, runtime_config=RuntimeConfig(paper=True, paper_mock=True))


def configuration(client):
    session = client.get("/api/session").json()["session"]
    return {
        "settings": settings_data(),
        "confirmed": True,
        "session_id": session["session_id"],
        "style_revision": session["style_revision"],
    }


def operation(client, account, revision=None):
    session = client.get("/api/session").json()["session"]
    return {
        "expected_revision": account["revision"] if revision is None else revision,
        "confirmed": True,
        "account_ref": account["account_ref"],
        "activation_revision": account["activation_revision"],
        "style_revision": session["style_revision"],
    }


def setup(client):
    auth = headers(client)
    assert (
        client.post(
            "/api/sessions", headers=auth, json={"style_strength": 35, "style_confirmed": True}
        ).status_code
        == 201
    )
    response = client.post(
        "/api/controls/trader",
        headers=auth,
        json={
            "enabled": True,
            "mode": "auto",
            "execution_environment": "paper",
            "confirmed": True,
            "expected_revision": 0,
        },
    )
    assert response.status_code == 200
    return auth


def test_configure_start_pause_and_reopen_show_only_virtual_funds(tmp_path):
    path = tmp_path / "paper.sqlite3"
    app = configured_app(path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        auth = setup(client)
        created = client.post(
            "/api/paper/configure",
            headers=auth,
            json=configuration(client),
        )
        assert created.status_code == 200
        account = created.json()["account"]
        assert account["usdt"] == "1000" and account["btc"] == "0"
        started = client.post(
            "/api/paper/start",
            headers=auth,
            json=operation(client, account),
        )
        assert started.status_code == 200
        client.portal.call(app.state.services.paper.step)
        snapshot = client.get("/api/paper").json()
        assert snapshot["cycles"][0]["status"] == "filled"
        assert snapshot["real_orders_enabled"] is False and snapshot["paid_models_enabled"] is False
        assert snapshot["decision_source"] == "offline_mock"
        revision = snapshot["account"]["revision"]
        assert (
            client.post(
                "/api/paper/pause",
                headers=auth,
                json=operation(client, snapshot["account"], revision),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/paper/configure",
                headers=auth,
                json=configuration(client),
            ).status_code
            == 409
        )
    with TestClient(configured_app(path), base_url="http://127.0.0.1") as client:
        headers(client)
        recovered = client.get("/api/paper").json()
        assert recovered["account"]["status"] == "paused"
        assert recovered["account"]["btc"] == snapshot["account"]["btc"]


@pytest.mark.parametrize("route", ["configure", "start", "pause"])
def test_every_paper_write_requires_same_origin_csrf_and_confirmation(tmp_path, route):
    with TestClient(
        configured_app(tmp_path / "paper.sqlite3"), base_url="http://127.0.0.1"
    ) as client:
        auth = setup(client)
        body = (
            {"settings": settings_data(), "confirmed": True}
            if route == "configure"
            else {"expected_revision": 1, "confirmed": True}
        )
        assert client.post("/api/paper/" + route, json=body).status_code == 403
        assert (
            client.post(
                "/api/paper/" + route, headers=auth | {"Origin": "http://evil.example"}, json=body
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/paper/" + route, headers=auth, json=body | {"confirmed": False}
            ).status_code
            == 422
        )


def test_default_paper_is_disabled_and_schema_rejects_coerced_revision(tmp_path):
    with TestClient(
        create_app(tmp_path / "default.sqlite3"), base_url="http://127.0.0.1"
    ) as client:
        auth = headers(client)
        assert client.get("/api/paper").json()["enabled"] is False
        assert (
            client.post(
                "/api/paper/start", headers=auth, json={"confirmed": True, "expected_revision": "1"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/paper/start",
                headers=auth,
                json={
                    "confirmed": True,
                    "expected_revision": 1,
                    "account_ref": "paper:missing",
                    "activation_revision": 1,
                    "style_revision": 1,
                },
            ).status_code
            == 503
        )
